"""Explicit, reproducible offline preparation; never invoked by the web server.

pip install -r requirements-embeddings.txt
python scripts/build_embeddings.py --model-dir ../../work/model-cache
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
from contractor_matching.data_loader import load_profiles
from contractor_matching.embeddings import MODEL, REVISION, TEXT_VERSION, digest, profile_text, query_key, query_space, query_text

EXPECTED = {
    'onnx/model_quantized.onnx': 'f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193',
    'tokenizer.json': '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Download the pinned public model if absent')
    args = parser.parse_args()
    for name, expected in EXPECTED.items():
        path = args.model_dir / name
        if not path.exists() and args.download:
            import httpx
            path.parent.mkdir(parents=True, exist_ok=True)
            with httpx.stream('GET', f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}', follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                with path.open('wb') as out:
                    for block in response.iter_bytes():
                        out.write(block)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Model checksum mismatch: {name}')
    tokenizer = Tokenizer.from_file(str(args.model_dir / 'tokenizer.json'))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(args.model_dir / 'onnx/model_quantized.onnx'), sess_options=options, providers=['CPUExecutionProvider'])

    def encode(text):
        encoded = tokenizer.encode(text)
        if len(encoded.ids) > 512:
            raise ValueError('Chunk exceeded model limit')
        inputs = {
            'input_ids': np.array([encoded.ids], dtype=np.int64),
            'attention_mask': np.array([encoded.attention_mask], dtype=np.int64),
            'token_type_ids': np.array([encoded.type_ids], dtype=np.int64),
        }
        hidden = session.run(['last_hidden_state'], inputs)[0][0]
        mask = np.array(encoded.attention_mask, dtype=np.float64)
        vector = (hidden.astype(np.float64) * mask[:, None]).sum(axis=0) / mask.sum()
        return vector / np.linalg.norm(vector), len(encoded.ids)

    def chunks(profile, words):
        text = profile_text(profile, ' '.join(words))
        if len(tokenizer.encode(text).ids) <= 512:
            return [text]
        mid = len(words) // 2
        if mid == 0:
            raise ValueError('Profile text cannot be chunked')
        return chunks(profile, words[:mid]) + chunks(profile, words[mid:])

    profiles = load_profiles(ROOT / 'data')
    artifact = {'model': MODEL, 'revision': REVISION, 'dimension': 384, 'text_version': TEXT_VERSION, 'profiles': {}, 'queries': {}}
    for index, p in enumerate(sorted(profiles, key=lambda p: p.id)):
        texts = chunks(p, p.description.split())
        pairs = [encode(text) for text in texts]
        vector = sum(v * weight for v, weight in pairs) / sum(weight for _, weight in pairs)
        vector /= np.linalg.norm(vector)
        artifact['profiles'][p.id] = {
            'description_sha256': digest(p.description), 'input_sha256': digest(profile_text(p)),
            'chunk_count': len(texts), 'vector': [round(float(x), 8) for x in vector],
        }
        if (index + 1) % 10 == 0:
            print(f'Profiles {index + 1}/{len(profiles)}', flush=True)
    for index, args_q in enumerate(query_space()):
        text = query_text(*args_q)
        vector, _ = encode(text)
        artifact['queries'][query_key(*args_q)] = {'input_sha256': digest(text), 'vector': [round(float(x), 8) for x in vector]}
        if (index + 1) % 100 == 0:
            print(f'Queries {index + 1}/408', flush=True)
    raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    (ROOT / 'data/embeddings.json').write_bytes(raw)
    manifest = {
        'artifact_sha256': hashlib.sha256(raw).hexdigest(), 'model': MODEL, 'revision': REVISION,
        'source_model': 'intfloat/multilingual-e5-small', 'license': 'MIT', 'dimension': 384,
        'profiles': len(profiles), 'queries': len(artifact['queries']), 'model_files_sha256': EXPECTED,
        'pooling': 'attention-mask mean pooling, L2; long descriptions recursively word-split below 512 tokens, token-weighted chunk mean, L2',
        'precision': 'quantized ONNX CPU, single input per inference; stored 8 decimal places, runtime renormalization',
        'build_versions': {'onnxruntime': ort.__version__, 'numpy': np.__version__},
        'sources': [f'https://huggingface.co/{MODEL}/tree/{REVISION}', 'https://huggingface.co/intfloat/multilingual-e5-small'],
    }
    (ROOT / 'data/embeddings_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'sha256': manifest['artifact_sha256'], 'bytes': len(raw)}), flush=True)


if __name__ == '__main__':
    main()
