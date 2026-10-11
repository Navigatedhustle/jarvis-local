FROM nvcr.io/nvidia/pytorch:25.11-py3
# Separate image: never upgrades the working speech or language containers.
RUN python -c "import importlib.metadata as m; open('/tmp/torch-constraints.txt','w').write('\n'.join(p+'=='+m.version(p) for p in ['torch','torchvision']))"
RUN pip install -c /tmp/torch-constraints.txt 'diffusers==0.36.0' 'transformers==4.57.1' 'accelerate==1.11.0' 'huggingface-hub==0.36.0' pillow safetensors
ENV HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HOME=/models/cache
WORKDIR /app
