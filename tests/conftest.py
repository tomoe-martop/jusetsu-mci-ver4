import os
import sys

# main.py と同じく api/ を import パスに入れる（egpf_common は api/ 配下の単一ファイル）
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_DIR = os.path.join(_BASE_DIR, "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)
