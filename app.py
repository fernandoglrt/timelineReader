import os
import re
import uuid
import json
import asyncio
import hashlib
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, abort, send_file, jsonify

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = Path('uploads')
app.config['BOOKS_FOLDER'] = Path('books')
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

ALLOWED_EXTENSIONS = {'.epub', '.pdf'}

TTS_CACHE = Path('tts_cache')

for folder in (app.config['UPLOAD_FOLDER'], app.config['BOOKS_FOLDER'], TTS_CACHE):
    folder.mkdir(exist_ok=True)

# Edge TTS neural voices per language (ISO 639-1)
EDGE_VOICES = {
    'pt': 'pt-BR-FranciscaNeural',
    'en': 'en-US-JennyNeural',
    'es': 'es-ES-ElviraNeural',
    'fr': 'fr-FR-DeniseNeural',
    'de': 'de-DE-KatjaNeural',
    'it': 'it-IT-ElsaNeural',
    'ja': 'ja-JP-NanamiNeural',
    'zh': 'zh-CN-XiaoxiaoNeural',
    'ru': 'ru-RU-SvetlanaNeural',
    'ar': 'ar-SA-ZariyahNeural',
    'ko': 'ko-KR-SunHiNeural',
    'nl': 'nl-NL-ColetteNeural',
}

# BCP-47 tags for Web Speech API, keyed by ISO 639-1 code
LANG_BCP47 = {
    'pt': 'pt-BR', 'en': 'en-US', 'es': 'es-ES', 'fr': 'fr-FR',
    'de': 'de-DE', 'it': 'it-IT', 'ja': 'ja-JP', 'zh': 'zh-CN',
    'ru': 'ru-RU', 'ar': 'ar-SA', 'ko': 'ko-KR', 'nl': 'nl-NL',
    'pl': 'pl-PL', 'tr': 'tr-TR', 'sv': 'sv-SE', 'da': 'da-DK',
    'fi': 'fi-FI', 'nb': 'nb-NO', 'cs': 'cs-CZ', 'ro': 'ro-RO',
}

try:
    from langdetect import detect as _langdetect
    def detect_lang(text: str) -> str:
        try:
            return _langdetect(text[:600]) or 'unknown'
        except Exception:
            return 'unknown'
except ImportError:
    def detect_lang(text: str) -> str:
        return 'unknown'


def resolve_lang(code: str) -> dict:
    """Normalise a raw lang tag to ISO 639-1 + BCP-47."""
    if not code:
        return {'iso': 'unknown', 'bcp47': 'unknown'}
    iso = code.split('-')[0].lower()[:2]
    return {'iso': iso, 'bcp47': LANG_BCP47.get(iso, iso)}


def parse_epub(filepath: Path) -> dict:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(filepath))

    meta_title  = book.get_metadata('DC', 'title')
    title       = meta_title[0][0] if meta_title else filepath.stem

    meta_author = book.get_metadata('DC', 'creator')
    author      = meta_author[0][0] if meta_author else ''

    # Language from metadata (most reliable source)
    meta_lang = book.get_metadata('DC', 'language')
    raw_lang  = meta_lang[0][0] if meta_lang else ''

    KEEP_TAGS = {
        'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'em', 'strong', 'i', 'b', 'blockquote',
        'ul', 'ol', 'li', 'br', 'hr', 'span',
        'figure', 'figcaption',
    }

    spine_ids = [item[0] for item in book.spine]
    ordered_items = []
    for sid in spine_ids:
        item = book.get_item_with_id(sid)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            ordered_items.append(item)
    if not ordered_items:
        ordered_items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    sections   = []
    text_sample = ''

    for item in ordered_items:
        try:
            raw = item.get_content().decode('utf-8', errors='replace')
        except Exception:
            continue

        soup = BeautifulSoup(raw, 'lxml')
        for tag in soup(['script', 'style', 'meta', 'link', 'head']):
            tag.decompose()

        body = soup.find('body') or soup

        for tag in body.find_all(True):
            if tag.name not in KEEP_TAGS:
                tag.unwrap()
        for tag in body.find_all(True):
            tag.attrs = {}

        html = str(body)
        text = body.get_text(strip=True)

        if len(text) > 80:
            sections.append({'html': html})
            if len(text_sample) < 600:
                text_sample += ' ' + text

    # Detect language from text if metadata was absent
    if not raw_lang and text_sample.strip():
        raw_lang = detect_lang(text_sample.strip())

    lang = resolve_lang(raw_lang)
    return {'title': title, 'author': author, 'sections': sections,
            'format': 'epub', 'lang': lang}


def parse_pdf(filepath: Path) -> dict:
    import fitz

    doc    = fitz.open(str(filepath))
    title  = doc.metadata.get('title',    '').strip() or filepath.stem
    author = doc.metadata.get('author',   '').strip()
    raw_lang = doc.metadata.get('language', '').strip()

    sections    = []
    text_sample = ''

    for page_num in range(len(doc)):
        page = doc[page_num]
        try:
            data = page.get_text("dict")
        except Exception:
            continue

        html_parts = []
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = block.get("lines", [])
            if not lines:
                continue
            all_spans = [s for line in lines for s in line.get("spans", [])]
            if not all_spans:
                continue

            avg_size = sum(s.get("size", 12) for s in all_spans) / len(all_spans)
            is_bold  = any("Bold" in s.get("font", "") for s in all_spans)
            line_texts = [
                " ".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                for ln in lines
            ]
            text = " ".join(t for t in line_texts if t).strip()
            if not text:
                continue

            if avg_size >= 15 or (is_bold and len(text) < 120):
                tag = "h2" if avg_size >= 18 else "h3"
                html_parts.append(f"<{tag}>{text}</{tag}>")
            else:
                html_parts.append(f"<p>{text}</p>")

        if html_parts:
            raw_text = "\n".join(html_parts)
            sections.append({'html': raw_text})
            if len(text_sample) < 600:
                text_sample += ' ' + " ".join(line_texts)

    doc.close()

    if not raw_lang and text_sample.strip():
        raw_lang = detect_lang(text_sample.strip())

    lang = resolve_lang(raw_lang)
    return {'title': title, 'author': author, 'sections': sections,
            'format': 'pdf', 'lang': lang}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return redirect(url_for('index'))

    file = request.files['file']
    if not file or not file.filename:
        return redirect(url_for('index'))

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return render_template('index.html', error="Formato inválido. Envie um arquivo .epub ou .pdf.")

    book_id  = str(uuid.uuid4())
    tmp_path = app.config['UPLOAD_FOLDER'] / f'{book_id}{ext}'

    try:
        file.save(tmp_path)

        if ext == '.epub':
            book_data = parse_epub(tmp_path)
        else:
            book_data = parse_pdf(tmp_path)

        if not book_data['sections']:
            return render_template('index.html', error="Não foi possível extrair conteúdo do arquivo.")

        book_data['id']                = book_id
        book_data['original_filename'] = file.filename

        book_path = app.config['BOOKS_FOLDER'] / f'{book_id}.json'
        with open(book_path, 'w', encoding='utf-8') as f:
            json.dump(book_data, f, ensure_ascii=False)

    except Exception as e:
        return render_template('index.html', error=f"Erro ao processar arquivo: {e}")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return redirect(url_for('reader', book_id=book_id))


@app.route('/delete/<book_id>', methods=['POST'])
def delete_book(book_id):
    try:
        uuid.UUID(book_id)
    except ValueError:
        abort(404)
    book_path = app.config['BOOKS_FOLDER'] / f'{book_id}.json'
    if book_path.exists():
        book_path.unlink()
    return ('', 204)   # No Content — caller handles UI update via JS


@app.route('/reader/<book_id>')
def reader(book_id):
    try:
        uuid.UUID(book_id)
    except ValueError:
        abort(404)

    book_path = app.config['BOOKS_FOLDER'] / f'{book_id}.json'
    if not book_path.exists():
        abort(404)

    with open(book_path, 'r', encoding='utf-8') as f:
        book = json.load(f)

    return render_template('reader.html', book=book)


def _prune_tts_cache(max_mb=80):
    mp3s = sorted(TTS_CACHE.glob('*.mp3'), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in mp3s)
    while total > max_mb * 1024 * 1024 and mp3s:
        oldest = mp3s.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)
        oldest.with_suffix('.json').unlink(missing_ok=True)


@app.route('/api/tts', methods=['POST'])
def api_tts():
    data  = request.get_json(silent=True) or {}
    text  = (data.get('text') or '').strip()[:3000]
    lang  = (data.get('lang') or 'en').split('-')[0].lower()
    rate  = float(data.get('rate', 1.0))
    if not text:
        abort(400)

    key       = hashlib.md5(f"tts2|{lang}|{rate}|{text}".encode()).hexdigest()
    mp3_path  = TTS_CACHE / f"{key}.mp3"
    json_path = TTS_CACHE / f"{key}.json"

    if not mp3_path.exists():
        words     = []
        generated = False

        # ── 1. edge-tts (neural voices + precise word timestamps) ─────
        try:
            import edge_tts as _edge
            voice    = EDGE_VOICES.get(lang, 'en-US-JennyNeural')
            rate_pct = f"{int((rate - 1) * 100):+d}%"

            async def _edge_gen():
                communicate = _edge.Communicate(text, voice, rate=rate_pct)
                chunks = []
                async for chunk in communicate.stream():
                    if chunk['type'] == 'audio':
                        chunks.append(chunk['data'])
                    elif chunk['type'] == 'WordBoundary':
                        words.append({'w': chunk['text'],
                                      's': chunk['offset'] // 10000,
                                      'd': chunk['duration'] // 10000})
                mp3_path.write_bytes(b''.join(chunks))

            asyncio.run(_edge_gen())
            # Only accept if the file has real audio content (>100 bytes)
            if mp3_path.exists() and mp3_path.stat().st_size > 100:
                generated = True
            else:
                mp3_path.unlink(missing_ok=True)
                words = []
        except Exception:
            mp3_path.unlink(missing_ok=True)
            words = []

        # ── 2. gTTS fallback (Google TTS, leve, sem timestamps precisos) ─
        if not generated:
            try:
                from gtts import gTTS
                safe_lang = lang if lang != 'unknown' else 'en'
                gTTS(text, lang=safe_lang, slow=False).save(str(mp3_path))
                # Estima timings a ~140 wpm
                raw_words = re.findall(r'\S+', text)
                ms_each   = max(1, int(60000 / (140 * rate)))
                words = [{'w': w, 's': i * ms_each, 'd': ms_each}
                         for i, w in enumerate(raw_words)]
                generated = True
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        if not generated:
            abort(503)

        _prune_tts_cache()
        json_path.write_text(json.dumps(words, ensure_ascii=False))

    words = json.loads(json_path.read_text()) if json_path.exists() else []
    return jsonify({'audio': f'/api/tts/audio/{key}', 'words': words})


@app.route('/api/tts/audio/<key>')
def api_tts_audio(key):
    if len(key) != 32 or not all(c in '0123456789abcdef' for c in key):
        abort(404)
    path = TTS_CACHE / f"{key}.mp3"
    if not path.exists():
        abort(404)
    try:
        return send_file(path, mimetype='audio/mpeg')
    except FileNotFoundError:
        abort(404)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
