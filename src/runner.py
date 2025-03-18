import os
from poc.create_vectorstore import create_faiss, create_chroma
from poc.load_vectorstore import load_faiss, load_chroma

if not os.path.exists("./src/common/faiss-vme"):
    create_faiss()

if not os.path.exists("./src/common/chroma-vme"):
    create_chroma()

faiss = load_faiss()
chroma = load_chroma()
