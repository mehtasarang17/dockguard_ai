import os
import sys
from markitdown import MarkItDown

def test_extract(filepath):
    md = MarkItDown()
    result = md.convert(filepath)
    print("Content Length:", len(result.text_content))
    print("First 1000 chars:")
    print(result.text_content[:1000])

if __name__ == "__main__":
    test_extract(sys.argv[1])
