# Models, data and asset retrieval

Primary: Qwen/Qwen3.8-27B-FP8 through the vLLM model-service script on 8181. Background service is separate on 8184; embedding Qwen3-Embedding-0.6B-Q8_0.gguf on 8182 and reranker Qwen3-Reranker-0.6B-rank-q8_0.gguf on 8183. STT/TTS are separate container/model adapters on 8185/8186; production TTS scripts specify Chatterbox V3, with Kokoro used only for synthetic reference preparation. Inspect `infra/systemd/` and adapter modules for exact models. Model weights and the Windows deployment authority are excluded. Retrieve upstream model artifacts under their own license and install in separate private model storage.

Runtime downloads and external account connections are not automatic. Review the upstream model card/license, acquire assets using its official instructions, pin repository revision and record SHA-256 before use. Large binary assets stay outside Git, including Git LFS. The local operator map records already-installed paths for recovery; those paths are not uploaded.

Atlas synthetic data: run its seed-42 generator. CampusCompass fixtures and Beat the Guard missions contain fictional records. Genesis creates a fresh simulated world. Other missing input datasets/media must be supplied only with confirmed rights and synthetic/approved contents.

License references: [Meta Llama 3.1](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct/blob/main/LICENSE), [Ultralytics licensing](https://www.ultralytics.com/license), [llama.cpp](https://github.com/ggml-org/llama.cpp), and locally retained model cards/manifests where supplied. Model terms are distinct from application source rights.
