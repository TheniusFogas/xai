import os
import pypdf
import time
import re 
from gtts import gTTS 
from flask import Flask, render_template, request, send_from_directory
from werkzeug.utils import secure_filename
from google import genai
from google.genai.errors import APIError
import subprocess # REVENIM LA MODULUL PYTHON STANDARD
import uuid 


# --- Configurarea Flask ---
app = Flask(__name__)

# Configurare directoare
UPLOAD_FOLDER = 'uploads'
STATIC_FOLDER = 'static'
ALLOWED_EXTENSIONS = {'pdf', 'txt'} 

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['STATIC_FOLDER'] = STATIC_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_FOLDER, exist_ok=True)

# *** SETĂRI GEMINI ***
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 

try:
    if not GEMINI_API_KEY:
        print("[INIT] AVERTISMENT: GEMINI_API_KEY nu este setat în mediul Cloud.")
    client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"[INIT] Eroare la inițializarea clientului Gemini: {e}")

# --- Constante și Setări ---
MAX_RETRIES = 5      
RETRY_DELAY = 8      
MAX_CHARS_PER_SEGMENT_TTS = 4800 
PAUSE_BETWEEN_REQUESTS = 2 


# --- Funcții Utilitare (Fără Schimbări) ---

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(filepath, extension):
    text = ""
    if extension == 'pdf':
        try:
            reader = pypdf.PdfReader(filepath)
            for page in reader.pages:
                text += page.extract_text() or '' + "\n"
        except Exception as e:
            return f"Eroare la citirea PDF: Documentul este criptat sau corupt. Detalii: {e}"
    elif extension == 'txt':
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                text = f.read()
        except Exception as e:
            return f"Eroare la citirea TXT: {e}"
            
    return text

def simple_text_cleanup(text):
    text = re.sub(r'[^\w\s.,?!;:\u0102\u0103\u00C2\u00E2\u00CE\u00EE\u015E\u015F\u0218\u0219\u021A\u021B]+', ' ', text)
    return ' '.join(text.split())

def process_text_with_gemini(text_content):
    
    if not GEMINI_API_KEY:
        return "Eroare: Cheia API Gemini nu este setată în mediul Cloud.", None

    prompt = f"""
    Ești un expert în procesarea textului. Traduce textul următor în Română. Foarte Important: Înlătură simbolurile, formatele markdown, spațiile excesive și orice caracter non-text. Nu scurta, păstrează textul integral. Returnează DOAR textul final procesat.
    
    TEXTUL DE PROCESAT: 
    ---
    {text_content}
    ---
    """
    
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=prompt,
            )
            
            if not response.text:
                 return "Gemini a returnat un răspuns gol.", None
            return response.text, "Romanian"

        except APIError as e:
            error_details = str(e)
            
            if "503 UNAVAILABLE" in error_details and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                continue
            else:
                return f"Eroare Gemini API: {e}", None
        except Exception as e:
            return f"Eroare neașteptată în procesarea Gemini: {e}", None
            
    return f"Eroare Gemini: Modelul a rămas supraîncărcat după {MAX_RETRIES} încercări.", None


def generate_tts_audio(text_content, lang='ro'):
    """Generează fișierul MP3 vocal folosind gTTS cu FFmpeg direct via subprocess."""

    if not text_content.strip():
        return "Textul de sinteză este gol.", False
    
    segments = [text_content[i:i + MAX_CHARS_PER_SEGMENT_TTS] 
                for i in range(0, len(text_content), MAX_CHARS_PER_SEGMENT_TTS)]
    
    temp_files = []
    
    try:
        # 1. Generează fișiere MP3 temporare și creează un fișier de listă
        list_file_path = os.path.join(app.config['STATIC_FOLDER'], f"list_{uuid.uuid4().hex}.txt")
        
        with open(list_file_path, 'w') as f:
            for i, segment in enumerate(segments):
                temp_mp3_name = f"temp_segment_{uuid.uuid4().hex}.mp3"
                temp_path = os.path.join(app.config['STATIC_FOLDER'], temp_mp3_name)
                temp_files.append(temp_path)
                
                tts = gTTS(text=segment, lang=lang, slow=False)
                tts.save(temp_path)
                
                # Folosim calea absolută în lista FFmpeg pentru siguranță
                full_temp_path = os.path.abspath(temp_path)
                f.write(f"file '{full_temp_path}'\n") 
                
                if i < len(segments) - 1:
                    time.sleep(PAUSE_BETWEEN_REQUESTS)

        # 2. Concatenează fișierele cu FFmpeg
        final_filename = f"tts_{uuid.uuid4().hex}.mp3"
        final_output_path = os.path.join(app.config['STATIC_FOLDER'], final_filename)
        
        # Comanda FFmpeg pentru concatenare (folosește 'concat' demuxer)
        command = [
            'ffmpeg',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file_path,
            '-c', 'copy',
            final_output_path
        ]
        
        # Rulează comanda FFmpeg
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        
        return final_filename, True

    except subprocess.CalledProcessError as e:
        error_msg = f"Eroare FFmpeg la rulare: FFmpeg a returnat eroare. Log-uri: {e.stderr.decode()}"
        return error_msg, False
    except FileNotFoundError:
        # Aceasta se întâmplă dacă binarul 'ffmpeg' nu este în PATH (problema majoră de instalare)
        return "Eroare FATALĂ: FFmpeg nu a fost găsit. Vă rog să verificați comanda de compilare Render.", False
    except subprocess.TimeoutExpired:
        return "Eroare: Operațiunea FFmpeg a expirat (Timeout de 60s). Documentul este prea lung.", False
    except Exception as e:
        return f"Eroare la generarea vocală (gTTS/Concatenare): {e}", False
    finally:
        # Curățarea fișierelor temporare și a listei
        for f in temp_files:
            if os.path.exists(f):
                os.remove(f)
        if os.path.exists(list_file_path):
             os.remove(list_file_path)


# --- Rute Flask (Server) ---

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    audio_file = None
    error_message = None
    should_translate = False
    
    # 1. LOGICA CERERII POST 
    if request.method == 'POST':
        should_translate = request.form.get('translate_checkbox') == 'on'
        tts_language = request.form.get('tts_language', 'ro')
        
        if 'document' not in request.files:
            error_message = 'Nu a fost găsit niciun fișier în cerere.'
        else:
            file = request.files['document']
            if file.filename == '' or not file or not allowed_file(file.filename):
                error_message = 'Tipul de fișier nu este permis.'
            else:
                filename = secure_filename(file.filename)
                file_extension = filename.rsplit('.', 1)[1].lower()
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    
                try:
                    file.save(filepath)
                    text_content = extract_text_from_file(filepath, file_extension)
                    os.remove(filepath)
                        
                    processed_text = ""
                        
                    if should_translate:
                        processed_text, target_lang = process_text_with_gemini(text_content)
                        if target_lang is None:
                            error_message = processed_text 
                        tts_language = 'ro' 
                    else:
                        processed_text = simple_text_cleanup(text_content)
                        tts_language = request.form.get('tts_language', 'ro') 
                        
                    if not processed_text.strip():
                        error_message = "Documentul este gol sau nu conține text selectabil."
                    else:
                        if not error_message: 
                            result, success = generate_tts_audio(processed_text, tts_language)
                            
                            if success:
                                audio_file = result
                            else:
                                error_message = result
                                    
                except Exception as e:
                    error_message = f"Eroare neașteptată de procesare pe server: {e}"

    # 2. LOGICA CERERII GET (Inițializarea paginii sau returnarea rezultatului)
    return render_template('index.html', audio_file=audio_file, error_message=error_message,
                           should_translate=should_translate)


@app.route('/static/<filename>')
def serve_audio(filename):
    """Permite servirea fișierului audio generat (MP3)."""
    return send_from_directory(app.config['STATIC_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=False, threaded=False)
