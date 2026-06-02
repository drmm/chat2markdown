import os
import re
import time
from pathlib import Path
from dotenv import load_dotenv

from chatmain_config import load_config

# -------- CONFIG --------
# Support multiple providers and API keys
GOOGLE_API_KEYS: list[str] = []
ZAI_API_KEY = ""
GOOGLE_MODEL = ""
GLM_MODEL = ""
OLLAMA_MODEL = ""
OLLAMA_URL = ""
MAX_INPUT_CHARS = 20000
REQUEST_DELAY_SEC = 1.5
MAX_RETRIES = 3
FILE_PATH = Path(__file__).resolve().parent / "testfile.md"

# Track which API key/provider to use
_current_google_key_index = 0
_use_glm = False  # Switch to GLM when Google is exhausted
# ------------------------


def configure_titles(config_ini: str | None = None) -> None:
    global GOOGLE_API_KEYS, ZAI_API_KEY, GOOGLE_MODEL, GLM_MODEL
    global OLLAMA_MODEL, OLLAMA_URL, MAX_INPUT_CHARS, REQUEST_DELAY_SEC
    global MAX_RETRIES, FILE_PATH

    cfg = load_config(config_ini)
    env_file = cfg.resolve_path(cfg.get("titles", "env_file", "./.env"))
    load_dotenv(dotenv_path=env_file)
    GOOGLE_API_KEYS = [k.strip() for k in os.environ.get("GOOGLE_API", "").split(",") if k.strip()]
    ZAI_API_KEY = os.environ.get("ZAI_API", "").strip()
    GOOGLE_MODEL = cfg.get("titles", "google_model", "gemini-2.5-flash")
    GLM_MODEL = cfg.get("titles", "glm_model", "GLM-4.6")
    OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", cfg.get("titles", "ollama_model", "qwen2.5:0.5b"))
    OLLAMA_URL = os.environ.get("OLLAMA_URL", cfg.get("titles", "ollama_url", "http://localhost:11434"))
    MAX_INPUT_CHARS = cfg.get_int("titles", "max_input_chars", 20000)
    REQUEST_DELAY_SEC = cfg.get_float("titles", "request_delay_sec", 1.5)
    MAX_RETRIES = cfg.get_int("titles", "max_retries", 3)
    FILE_PATH = cfg.resolve_path(cfg.get("titles", "test_file", "./testfile.md"))


configure_titles()

PROMPT_TEMPLATE = (
    "Write a short title to be used as a filename that describes what conversation is about. "
    "The title should read like a brief narrative or task summary, not a command. "
    "Keep it under 100 characters. Do not use quotes, emojis, or ending punctuation. "
    "Be specific but concise.\n\n"
    "Conversation:\n"
)

def read_chat_file(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    if len(text) > MAX_INPUT_CHARS:
        text = text[-MAX_INPUT_CHARS:]  # keep most recent content
    return text

def call_google_api(prompt: str) -> str:
    """Call Google Gemini API."""
    global _current_google_key_index
    import google.generativeai as genai
    
    api_key = GOOGLE_API_KEYS[_current_google_key_index % len(GOOGLE_API_KEYS)]
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GOOGLE_MODEL)
    
    response = model.generate_content(prompt)
    
    # Get text from response parts
    text = ""
    for part in response.parts:
        text += part.text
    return text.strip()

def call_glm_api(prompt: str) -> str:
    """Call Zhipu AI GLM API."""
    from zhipuai import ZhipuAI
    
    client = ZhipuAI(api_key=ZAI_API_KEY)
    response = client.chat.completions.create(
        model=GLM_MODEL,
        messages=[
            {"role": "user", "content": prompt}
        ],
        max_tokens=150,
        temperature=0.3
    )
    return response.choices[0].message.content.strip()

def call_ollama_api(prompt: str) -> str:
    """Call local Ollama API (phi3 or other local models)."""
    from ollama_client import OllamaClient
    
    client = OllamaClient(base_url=OLLAMA_URL, model=OLLAMA_MODEL)
    
    if not client.is_available():
        raise RuntimeError("Ollama server is not running")
    
    response = client.generate(
        prompt=prompt,
        temperature=0.3,
        max_tokens=150
    )
    return response.strip()

def is_ollama_available() -> bool:
    """Check if Ollama is running locally."""
    try:
        from ollama_client import OllamaClient
        client = OllamaClient(base_url=OLLAMA_URL, model=OLLAMA_MODEL)
        return client.is_available()
    except Exception:
        return False

def get_title_for_chat(file_path: str, prefer_local: bool = True) -> str:
    global _current_google_key_index, _use_glm
    
    chat_text = read_chat_file(file_path)
    prompt = PROMPT_TEMPLATE + chat_text
    
    last_error = None
    
    # Try Ollama first if prefer_local is True and it's available
    if prefer_local and is_ollama_available():
        try:
            print(f"  Using local Ollama ({OLLAMA_MODEL})...")
            text = call_ollama_api(prompt)
            return clean_title(text)
        except Exception as e:
            print(f"  Ollama failed: {str(e)[:50]}... trying cloud providers")
            last_error = e
    
    if not GOOGLE_API_KEYS and not ZAI_API_KEY:
        # No cloud keys, try Ollama even if prefer_local is False
        if is_ollama_available():
            try:
                print(f"  No cloud API keys, using local Ollama ({OLLAMA_MODEL})...")
                text = call_ollama_api(prompt)
                return clean_title(text)
            except Exception as e:
                last_error = e
        raise ValueError("No API keys set and Ollama not available. Add GOOGLE_API or ZAI_API to .env file, or start Ollama.")
    
    # Try Google first (if available and not exhausted)
    if GOOGLE_API_KEYS and not _use_glm:
        for retry in range(MAX_RETRIES):
            try:
                text = call_google_api(prompt)
                time.sleep(REQUEST_DELAY_SEC)
                return clean_title(text)
                
            except Exception as e:
                last_error = e
                error_str = str(e)
                
                # Check if rate limited
                if "429" in error_str or "quota" in error_str.lower():
                    _current_google_key_index += 1
                    
                    if _current_google_key_index >= len(GOOGLE_API_KEYS):
                        print(f"  Google quota exhausted, switching to GLM...")
                        _use_glm = True
                        break
                    else:
                        wait_time = 5
                        match = re.search(r'retry in (\d+)', error_str.lower())
                        if match:
                            wait_time = min(int(match.group(1)) + 2, 30)
                        print(f"  Rate limited, trying Google key {_current_google_key_index + 1}/{len(GOOGLE_API_KEYS)} after {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                else:
                    # Other error, try GLM
                    print(f"  Google error: {str(e)[:50]}... trying GLM")
                    _use_glm = True
                    break
    
    # Try GLM as fallback
    if ZAI_API_KEY:
        for retry in range(MAX_RETRIES):
            try:
                text = call_glm_api(prompt)
                time.sleep(REQUEST_DELAY_SEC)
                return clean_title(text)
                
            except Exception as e:
                last_error = e
                error_str = str(e)
                
                if "429" in error_str or "rate" in error_str.lower():
                    wait_time = 10
                    print(f"  GLM rate limited, waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise
    
    # Fall back to local extraction if all LLM providers fail
    print(f"  Cloud LLM providers failed, trying Ollama...")
    
    # Last resort: try Ollama even if prefer_local was False
    if is_ollama_available():
        try:
            text = call_ollama_api(prompt)
            return clean_title(text)
        except Exception as e:
            print(f"  Ollama also failed: {str(e)[:50]}")
    
    print(f"  All LLM providers failed, using local extraction...")
    return extract_title_locally(file_path)

def extract_title_locally(file_path: str) -> str:
    """Extract a title from file content without using an LLM."""
    import re
    from pathlib import Path
    
    content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    
    # Try to find user messages
    user_msgs = re.findall(r'## User\n(.+?)(?=\n##|\n---|\Z)', content, re.DOTALL)
    if user_msgs:
        first_msg = user_msgs[0].strip()
    else:
        # Fall back to first non-header content
        lines = [l for l in content.split('\n') if l.strip() and not l.startswith('#') and not l.startswith('-')]
        first_msg = lines[0] if lines else content[:100]
    
    # Clean and truncate
    first_msg = re.sub(r'[^\w\s]', ' ', first_msg)
    first_msg = ' '.join(first_msg.split()[:10])
    
    # Capitalize first letter of each word
    title = ' '.join(word.capitalize() for word in first_msg.split())
    
    return title[:80] if title else "Untitled Chat"

def clean_title(text: str) -> str:
    """Clean up the generated title."""
    text = text.strip()
    
    # If multiple options returned, take the first one
    if text.startswith("*"):
        lines = [l.strip().lstrip("*").strip() for l in text.split("\n") if l.strip()]
        text = lines[0] if lines else text
    
    # Remove quotes if wrapped
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    
    return text

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate title for chat file")
    parser.add_argument("--config-ini", "--ini", dest="config_ini", default=None,
                        help="Config file to read before applying CLI overrides.")
    parser.add_argument("--cloud", action="store_true", help="Prefer cloud APIs over local Ollama")
    parser.add_argument("--file", type=str, default=None, help="Chat file to process")
    args = parser.parse_args()
    configure_titles(args.config_ini)
    
    try:
        ollama_status = "Yes" if is_ollama_available() else "No"
        print(f"Providers: Ollama={ollama_status} ({OLLAMA_MODEL}), Google={len(GOOGLE_API_KEYS)} keys, GLM={'Yes' if ZAI_API_KEY else 'No'}")
        
        prefer_local = not args.cloud
        title = get_title_for_chat(args.file or str(FILE_PATH), prefer_local=prefer_local)
        print(f"Generated title: {title}")
    except Exception as e:
        print(f"Error: {e}")
