import os
from poc.create_vectorstore import create_faiss, create_chroma, create_weaviate
from poc.load_vectorstore import load_faiss, load_chroma, load_weaviate

# if not os.path.exists("./src/common/faiss-vme"):
#     create_faiss()

# if not os.path.exists("./src/common/chroma-vme"):
#     create_chroma()
    
# create_weaviate()

# load_faiss()
# load_chroma()
load_weaviate()
