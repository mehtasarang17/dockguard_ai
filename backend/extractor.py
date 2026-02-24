import os
import subprocess
from markitdown import MarkItDown

def extract_text(file_path: str) -> str:
    """Extract and convert a document to Markdown using MarkItDown."""
    ext = os.path.splitext(file_path)[1].lower()
    
    # MarkItDown natively supports DOCX, XLSX, PDF, TXT, CSV, etc.
    # However, it does NOT support the archaic `.doc` format natively.
    if ext == '.doc':
        return _extract_doc(file_path)
        
    try:
        # Initialize the parser
        md = MarkItDown()
        
        # Convert the document (supports PDF, DOCX, XLSX, etc. out of the box)
        result = md.convert(file_path)
        
        return result.text_content
    except Exception as e:
        raise ValueError(f"Failed to extract document '{file_path}': {e}")


def _extract_doc(file_path: str) -> str:
    """Extract text from legacy .doc files using antiword."""
    try:
        # -w 0 disables line wrapping for cleaner text
        result = subprocess.check_output(['antiword', '-w', '0', file_path], text=True)
        return result
    except FileNotFoundError:
        raise ValueError("System dependency 'antiword' not found. Please install it to support .doc files.")
    except subprocess.CalledProcessError as e:
        raise ValueError(f"Failed to extract .doc file: {e}")
