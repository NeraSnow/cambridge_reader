import os
import sys
import re
import urllib.parse
import requests
import hashlib
import json
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template, Response

# Force UTF-8 stdout encoding on Windows
if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__, template_folder='templates')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
}

BASE_URL = "https://dictionary.cambridge.org"

# Disk cache directories setup
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
DEFS_CACHE_DIR = os.path.join(CACHE_DIR, "definitions")
CSS_CACHE_DIR = os.path.join(CACHE_DIR, "css")
FONTS_CACHE_DIR = os.path.join(CACHE_DIR, "fonts")

for directory in [DEFS_CACHE_DIR, CSS_CACHE_DIR, FONTS_CACHE_DIR]:
    os.makedirs(directory, exist_ok=True)

def get_md5(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def rewrite_css_urls(css_content, css_base_url):
    """
    Finds all url(...) definitions in CSS. Resolves relative URLs to absolute,
    and routes font files through our font proxy to bypass CORS restrictions.
    """
    css_url_pattern = re.compile(r'url\s*\(\s*([^\)]+)\s*\)')
    
    def replace_url(match):
        raw_url = match.group(1).strip('\'" \t\r\n')
        if raw_url.startswith('data:'):
            return match.group(0)
            
        full_url = urllib.parse.urljoin(css_base_url, raw_url)
        parsed = urllib.parse.urlparse(full_url)
        path = parsed.path
        
        if any(path.lower().endswith(ext) for ext in ['.woff', '.woff2', '.ttf', '.eot', '.otf']):
            proxied_url = f"/api/font-proxy?url={urllib.parse.quote(full_url)}"
            return f"url('{proxied_url}')"
        else:
            return f"url('{full_url}')"
            
    return css_url_pattern.sub(replace_url, css_content)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/define')
def define_api():
    word = request.args.get('word', '').strip().lower()
    if not word:
        return jsonify({'error': 'No word provided'}), 400
        
    # Check definition cache on disk first
    cache_filename = get_md5(word) + ".json"
    cache_filepath = os.path.join(DEFS_CACHE_DIR, cache_filename)
    if os.path.exists(cache_filepath):
        try:
            with open(cache_filepath, 'r', encoding='utf-8') as f:
                cached_data = json.load(f)
                response = jsonify(cached_data)
                # Cache on the browser side for 1 day
                response.headers['Cache-Control'] = 'public, max-age=86400'
                return response
        except Exception as e:
            print(f"Error reading definition cache: {e}")

    encoded_word = urllib.parse.quote(word)
    url = f"{BASE_URL}/zhs/%E8%AF%8D%E5%85%B8/%E8%8B%B1%E8%AF%AD-%E6%B1%89%E8%AF%AD-%E7%AE%80%E4%BD%93/{encoded_word}"
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        
        if response.status_code == 404:
            return jsonify({'error': 'Word not found in the dictionary', 'suggestions': []}), 404
            
        soup = BeautifulSoup(response.text, 'html.parser')
        page_content = soup.find(id="page-content")
        
        if not page_content:
            suggestions = []
            ul_suggest = soup.find(class_="prefix-suggestions")
            if ul_suggest:
                suggestions = [a.get_text().strip() for a in ul_suggest.find_all('a') if a.get_text()]
            
            return jsonify({
                'error': 'Word not found directly. Please check spelling.',
                'suggestions': suggestions[:10]
            }), 404
            
        # Extract stylesheets and wrap them with our CSS proxy
        stylesheets = []
        for link in soup.find_all('link', rel='stylesheet'):
            href = link.get('href', '')
            if href:
                full_href = BASE_URL + href if href.startswith('/') else href
                stylesheets.append(f"/api/css-proxy?url={urllib.parse.quote(full_href)}")
                    
        stylesheets.append(f"/api/css-proxy?url={urllib.parse.quote('https://cdn.polarbyte.com/idm/cdo/iaw.min.css')}")
        
        # Decompose all <script> tags inside page_content to avoid executing tracking/ad scripts
        for script in page_content.find_all('script'):
            script.decompose()
        
        # Rewrite all dictionary hyperlinks to point to our local app routing
        for a in page_content.find_all('a'):
            href = a.get('href', '')
            if href:
                if '/dictionary/' in href or '/%E8%AF%8D%E5%85%B8/' in href or 'dictionary.cambridge.org' in href:
                    parsed_href = urllib.parse.urlparse(href)
                    parts = parsed_href.path.split('/')
                    target_word = parts.pop() or parts.pop()
                    if target_word and not target_word.endswith(('.css', '.js', '.png', '.jpg', '.mp3', '.ogg', '.woff', '.woff2')):
                        a['href'] = f"/?word={urllib.parse.unquote(target_word)}"
                        a['class'] = a.get('class', []) + ['clean-query-link']
        
        # Clean advertisement divs from the main content using strict class matching rules
        ad_classes = {'ad', 'amp-ad', 'i-amphtml-ad', 'display-ad', 'amp-embed', 'contentslot', 'am-default_moreslots'}
        for ad_el in page_content.find_all(class_=True):
            if ad_el.attrs is None:
                continue
                
            classes = ad_el.get('class', [])
            # Decompose ads by class
            if any(c in ad_classes or c.startswith('ad-') or 'contentslot' in c for c in classes):
                ad_el.decompose()
                continue
                
            # Decompose user-requested useless elements strictly:
            if 'lcs' in classes and 'bh' in classes:
                ad_el.decompose()
                continue
                
            if 'hax' in classes:
                if 'dwl' in classes or ad_el.find(class_=['hao', 'hbtn']):
                    ad_el.decompose()
                
        # Clean ads by ID
        for ad_el in page_content.find_all(id=True):
            if ad_el.attrs is None:
                continue
            id_val = ad_el.get('id', '')
            if id_val.startswith('ad-') or id_val.startswith('ad_') or 'google_ads' in id_val or 'contentslot' in id_val:
                ad_el.decompose()
                
        # Convert amp-accordion elements to native standard HTML5 details/summary tags
        for accordion in page_content.find_all('amp-accordion'):
            if accordion.attrs is None:
                continue
            section = accordion.find('section')
            if not section:
                continue
            details = soup.new_tag('details', attrs={'class': 'extra-examples-details'})
            header = section.find('header')
            if header:
                summary = soup.new_tag('summary', attrs={'class': 'extra-examples-summary'})
                for child in list(header.children):
                    summary.append(child)
                details.append(summary)
                header.decompose()
            for child in list(section.children):
                if child.name:
                    details.append(child)
            accordion.replace_with(details)
            
        # Rewrite all audio source relative URLs to our absolute proxy path
        for audio in page_content.find_all('audio'):
            for source in audio.find_all('source'):
                src = source.get('src', '')
                if src:
                    full_src = BASE_URL + src if src.startswith('/') else src
                    source['src'] = f"/api/audio-proxy?url={urllib.parse.quote(full_src)}"
                    
        # Rewrite data-src-mp3/data-src-ogg elements for the audio buttons
        for btn in page_content.find_all(attrs={"data-src-mp3": True}):
            src = btn['data-src-mp3']
            full_src = BASE_URL + src if src.startswith('/') else src
            btn['data-src-mp3'] = f"/api/audio-proxy?url={urllib.parse.quote(full_src)}"
            
        for btn in page_content.find_all(attrs={"data-src-ogg": True}):
            src = btn['data-src-ogg']
            full_src = BASE_URL + src if src.startswith('/') else src
            btn['data-src-ogg'] = f"/api/audio-proxy?url={urllib.parse.quote(full_src)}"
            
        data = {
            'word': word,
            'html': str(page_content),
            'stylesheets': stylesheets
        }
        
        # Write response to persistent disk cache
        try:
            with open(cache_filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"Error writing definition cache: {e}")
            
        api_response = jsonify(data)
        api_response.headers['Cache-Control'] = 'public, max-age=86400'
        return api_response
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Network error fetching definition: {str(e)}'}), 500

@app.route('/api/css-proxy')
def css_proxy():
    css_url = request.args.get('url', '')
    if not css_url:
        return "Missing URL", 400
        
    if 'polarbyte.com' not in css_url and 'cambridge.org' not in css_url:
        return "Invalid domain", 403
        
    cache_filename = get_md5(css_url) + ".css"
    cache_filepath = os.path.join(CSS_CACHE_DIR, cache_filename)
    
    # Check disk cache
    if os.path.exists(cache_filepath):
        try:
            with open(cache_filepath, 'r', encoding='utf-8') as f:
                cached_css = f.read()
                response = Response(cached_css, content_type='text/css')
                response.headers['Cache-Control'] = 'public, max-age=31536000' # 1 Year
                return response
        except Exception as e:
            print(f"Error reading CSS cache: {e}")
            
    try:
        res = requests.get(css_url, headers=HEADERS, timeout=10)
        cleaned_css = rewrite_css_urls(res.text, css_url)
        
        # Write to disk cache
        try:
            with open(cache_filepath, 'w', encoding='utf-8') as f:
                f.write(cleaned_css)
        except Exception as e:
            print(f"Error writing CSS cache: {e}")
            
        response = Response(cleaned_css, content_type='text/css')
        response.headers['Cache-Control'] = 'public, max-age=31536000'
        return response
    except Exception as e:
        return str(e), 500

@app.route('/api/font-proxy')
def font_proxy():
    font_url = request.args.get('url', '')
    if not font_url:
        return "Missing URL", 400
        
    if 'cambridge.org' not in font_url and 'polarbyte.com' not in font_url:
        return "Invalid domain", 403
        
    ext = urllib.parse.urlparse(font_url).path.split('.')[-1].lower()
    cache_filename = get_md5(font_url) + f".{ext}"
    cache_filepath = os.path.join(FONTS_CACHE_DIR, cache_filename)
    
    # Content type determination
    content_type = 'application/octet-stream'
    if ext == 'woff2':
        content_type = 'font/woff2'
    elif ext == 'woff':
        content_type = 'font/woff'
    elif ext == 'ttf':
        content_type = 'font/ttf'
        
    # Check disk cache
    if os.path.exists(cache_filepath):
        try:
            with open(cache_filepath, 'rb') as f:
                font_content = f.read()
                response = Response(font_content, content_type=content_type)
                response.headers['Access-Control-Allow-Origin'] = '*'
                response.headers['Cache-Control'] = 'public, max-age=31536000' # 1 Year
                return response
        except Exception as e:
            print(f"Error reading font cache: {e}")
            
    try:
        res = requests.get(font_url, headers=HEADERS, timeout=10)
        
        # Write to disk cache
        try:
            with open(cache_filepath, 'wb') as f:
                f.write(res.content)
        except Exception as e:
            print(f"Error writing font cache: {e}")
            
        response = Response(res.content, content_type=content_type)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'public, max-age=31536000'
        return response
    except Exception as e:
        return str(e), 500

@app.route('/api/audio-proxy')
def audio_proxy():
    audio_url = request.args.get('url', '')
    if not audio_url:
        return "Missing URL", 400
        
    if not audio_url.startswith(BASE_URL) and not audio_url.startswith("https://dictionary.cambridge.org"):
        return "Invalid domain", 403
        
    try:
        res = requests.get(audio_url, headers=HEADERS, timeout=5)
        response = Response(res.content, content_type=res.headers.get('content-type', 'audio/mpeg'))
        # Cache audio files in browser for 30 days
        response.headers['Cache-Control'] = 'public, max-age=2592000'
        return response
    except Exception as e:
        return str(e), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
