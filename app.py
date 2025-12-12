import os
import pypdf
import time
import re 
from gtts import gTTS 
from flask import Flask, render_template, request, send_from_directory
from werkzeug.utils import secure_filename
from google import genai
from google.genai.errors import APIError
from pydub import AudioSegment 
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
# Pe Render, cheia va fi preluată din variabila de mediu "GEMINI_API_KEY"
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


# --- Funcții Utilitare (Extract, Cleanup, Gemini - Fără Schimbări) ---

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(filepath, extension):
    text = ""
    print(f"[EXTRACT] Începe extragerea din fișierul de tip: {extension}")
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
        return "Eroare: Cheia API Gemini nu este setată în mediul de găzduire.", None

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


def generate_tts_audio(text_content, lang='ro', output_filename='audio_output.mp3'):
    """Generează fișierul MP3 vocal folosind gTTS (online) cu fragmentare și lipire (FFmpeg)."""

    if not text_content.strip():
        return "Textul de sinteză este gol.", False
    
    segments = [text_content[i:i + MAX_CHARS_PER_SEGMENT_TTS] 
                for i in range(0, len(text_content), MAX_CHARS_PER_SEGMENT_TTS)]
    
    combined_audio = AudioSegment.empty()
    temp_files = []
    
    try:
        for i, segment in enumerate(segments):
            temp_mp3_name = f"temp_segment_{uuid.uuid4().hex}.mp3"
            temp_path = os.path.join(app.config['STATIC_FOLDER'], temp_mp3_name)
            temp_files.append(temp_path)
            
            tts = gTTS(text=segment, lang=lang, slow=False)
            tts.save(temp_path)
            
            # Combinare (necesită FFmpeg care este implicit pe Render)
            combined_audio += AudioSegment.from_mp3(temp_path)
            
            if i < len(segments) - 1:
                time.sleep(PAUSE_BETWEEN_REQUESTS)

        # Salvarea fișierului final
        output_path = os.path.join(app.config['STATIC_FOLDER'], output_filename)
        # Nume fișier unic pentru a evita conflictele de cache
        final_filename = f"tts_{uuid.uuid4().hex}.mp3"
        final_output_path = os.path.join(app.config['STATIC_FOLDER'], final_filename)
        combined_audio.export(final_output_path, format="mp3")
        
        return final_filename, True

    except Exception as e:
        # Aici apare eroarea dacă FFmpeg lipsește pe Render.
        return f"Eroare la generarea vocală (TTS). Eroare FFmpeg: {e}", False
    finally:
        for f in temp_files:
            if os.path.exists(f):
                os.remove(f)


# --- Rute Flask (Server) ---

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    audio_file = None
    error_message = None
    
    should_translate = request.form.get('translate_checkbox') == 'on'
    
    if request.method == 'POST':
        tts_language = request.form.get('tts_language', 'ro')
        
        if 'document' not in request.files:
            error_message = 'Nu a fost găsit niciun fișier în cerere.'
            return render_template('index.html', error_message=error_message, should_translate=should_translate)
        
        file = request.files['document']
        if file.filename == '' or not file or not allowed_file(file.filename):
            error_message = 'Tipul de fișier nu este permis. Vă rugăm să folosiți .pdf sau .txt.'
            return render_template('index.html', error_message=error_message, should_translate=should_translate)

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
                    return render_template('index.html', error_message=error_message, should_translate=should_translate)
                tts_language = 'ro' 
            else:
                processed_text = simple_text_cleanup(text_content)
                tts_language = request.form.get('tts_language', 'ro') 
                
            if not processed_text.strip():
                error_message = "Documentul este gol sau nu conține text selectabil."
            else:
                result, success = generate_tts_audio(processed_text, tts_language)
                
                if success:
                    audio_file = result
                else:
                    error_message = result
                        
        except Exception as e:
            error_message = f"Eroare neașteptată de procesare pe server: {e}"
            
    return render_template('index.html', audio_file=audio_file, error_message=error_message,
                           should_translate=should_translate)

@app.route('/static/<filename>')
def serve_audio(filename):
    """Permite servirea fișierului audio generat (MP3)."""
    return send_from_directory(app.config['STATIC_FOLDER'], filename)

if __name__ == '__main__':
    # Pe Render, serverul Gunicorn sau uWSGI va rula aplicația, nu această linie.
    # Dar o păstrăm pentru testare locală.
    app.run(debug=False, threaded=False)
