"""Generate notebooks/voice_agent_colab.ipynb from the cell list below.

Keeping the notebook as generated source makes it reviewable and diffable.
  python tools/build_notebook.py          # writes + validates the notebook
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "notebooks" / "voice_agent_colab.ipynb"
INCLUDE_TEXT = "--with-text" in sys.argv     # default: code cells only (user preference, 2026-11-08)

CELLS: list[tuple[str, str, bool]] = []
# Cells marked extra=True (audit, tests, benchmarks, self-test) are left out of the notebook unless
# built with --full. The default notebook keeps only what runs the agent (user preference, 2026-11-08).
FULL = "--full" in sys.argv


def md(s: str, extra: bool = False) -> None:
    CELLS.append(("md", s.strip("\n"), extra))


def code(s: str, extra: bool = False) -> None:
    CELLS.append(("code", s.strip("\n"), extra))


# --------------------------------------------------------------------------------------------
md(r"""
# AI Voice Agent: Colab development environment (Phases 3–4)

**Development infrastructure, not production.** Colab VMs are recycled (idle timeout, ~12 h max),
have no inbound networking, and wipe `/content` on reset. Everything that must survive lives on
Google Drive under `MyDrive/voice-agent/`.

| Persists on Drive | Lost on runtime reset (rebuilt by re-running cells) |
|---|---|
| models, datasets, benchmark results, logs, secrets file, PostgreSQL **backups**, build caches (llama.cpp, Node, n8n, cloudflared, pip, HF) | PostgreSQL data dir (restored from latest backup), installed pip/apt packages, running services, tunnel URL |

**Run top to bottom.** Every cell is idempotent: re-running skips work already done.
**After a disconnect or reset:** run cells 1 → 14 again (minutes, thanks to the caches), then whatever you need from 15+.

Runtime: *Runtime → Change runtime type → T4 GPU*.

Status of each component is tracked in `docs/phase3-4-ai-pipeline.md`. No benchmark number exists until you run section 16 and it writes `results/phase4/`.
""")

md(r"""
## 1. Mount Google Drive
""")
code(r"""
from google.colab import drive
drive.mount('/content/drive')
import os
os.environ.setdefault('VOICE_AGENT_DRIVE', '/content/drive/MyDrive/voice-agent')
print('Drive root:', os.environ['VOICE_AGENT_DRIVE'])
""")

md(r"""
## 2. Project code + persistent directory layout

Code source, in order of preference:
1. **Git**: set Colab secret `GIT_REPO_URL` (e.g. `https://github.com/you/voice-agent.git`) and, for a private repo, `GIT_REPO_TOKEN` (fine-grained, read-only).
2. **Zip on Drive** (simplest): upload `voice-agent-code.zip` to `MyDrive/voice-agent/`. To update the code later, upload a new zip with the same name.
3. **Folder on Drive**: upload this project folder to `MyDrive/voice-agent/code_src/`.

The code is copied to fast local disk (`/content/runtime/code`) on each session.
""")
code(r"""
import os, shutil, subprocess, sys
from pathlib import Path

DRIVE = Path(os.environ['VOICE_AGENT_DRIVE'])
CODE = Path('/content/runtime/code')
CODE.parent.mkdir(parents=True, exist_ok=True)

def _secret(name):
    try:
        from google.colab import userdata
        return userdata.get(name)
    except Exception:
        return None

repo, token = _secret('GIT_REPO_URL'), _secret('GIT_REPO_TOKEN')
if repo:
    url = repo.replace('https://', f'https://x-access-token:{token}@') if token else repo
    if (CODE / '.git').exists():
        subprocess.run(['git', '-C', str(CODE), 'pull', '--ff-only', '-q'], check=True)
    else:
        shutil.rmtree(CODE, ignore_errors=True)
        r = subprocess.run(['git', 'clone', '-q', url, str(CODE)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError('git clone failed (check GIT_REPO_URL / GIT_REPO_TOKEN): ' + r.stderr.replace(token or '\0', '***'))
    print('code from git:', subprocess.run(['git', '-C', str(CODE), 'log', '-1', '--format=%h %s'], capture_output=True, text=True).stdout.strip())
elif (DRIVE / 'voice-agent-code.zip').exists():
    import zipfile
    shutil.rmtree(CODE, ignore_errors=True)
    with zipfile.ZipFile(DRIVE / 'voice-agent-code.zip') as zf:
        bad = [n for n in zf.namelist() if n.startswith('/') or '..' in Path(n).parts]
        if bad:
            raise ValueError(f'unsafe paths in zip: {bad[:3]}')
        zf.extractall(CODE)
    if not (CODE / 'voice_engine').exists():
        raise FileNotFoundError('voice-agent-code.zip does not contain the project (voice_engine/ missing at its top level)')
    print('code extracted from', DRIVE / 'voice-agent-code.zip')
elif (DRIVE / 'code_src' / 'voice_engine').exists():
    subprocess.run(['rsync', '-a', '--delete', '--exclude', '.git', f"{DRIVE / 'code_src'}/", f'{CODE}/'], check=True)
    print('code copied from', DRIVE / 'code_src')
else:
    raise FileNotFoundError(f"No code found: upload voice-agent-code.zip to {DRIVE}, "
                            f"or set GIT_REPO_URL, or upload the project folder to {DRIVE / 'code_src'}")

if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))
# forget previously imported project modules so re-running this cell picks up new code
for _m in [m for m in sys.modules if m.split('.')[0] in ('ops', 'voice_engine', 'bench', 'gateway')]:
    del sys.modules[_m]
os.chdir(CODE)
from ops import env as ops_env
ops_env.ensure_layout()
print('persistent :', ops_env.DRIVE_ROOT)
print('ephemeral  :', ops_env.RUNTIME)
""")

md(r"""
## 3. GPU detection
The voice pipeline needs CUDA. This cell stops the notebook if no GPU is attached.
""")
code(r"""
import subprocess
r = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,driver_version,compute_cap', '--format=csv,noheader'],
                   capture_output=True, text=True)
if r.returncode != 0:
    raise SystemError('No NVIDIA GPU. Runtime -> Change runtime type -> T4 GPU, then re-run from cell 1.')
print('GPU:', r.stdout.strip())
import torch
print('torch', torch.__version__, '| CUDA', torch.version.cuda, '| cuDNN', torch.backends.cudnn.version(),
      '| available', torch.cuda.is_available())
assert torch.cuda.is_available(), 'torch cannot see the GPU'
cc = torch.cuda.get_device_capability(0)
if cc != (7, 5):
    print(f'WARNING: compute capability {cc}; the llama.cpp build in cell 12 targets sm_75 (T4). '
          'Set LLAMA_CUDA_ARCH accordingly or use a T4.')
print(subprocess.run(['nvcc', '--version'], capture_output=True, text=True).stdout.splitlines()[-2])
""")

md(r"""
## 4. Disk space
Models (~10–15 GB for all candidates), datasets (~0.5 GB) and caches live on Drive; builds use local disk.
""")
code(r"""
from ops.env import disk_report
rep = disk_report()
print(rep)
if rep['vm_local']['free_gb'] < 20:
    raise SystemError('Less than 20 GB free on the VM disk; restart the runtime or free space.')
if rep.get('drive', {}).get('free_gb', 99) < 20:
    print('WARNING: under 20 GB free on Google Drive; downloading every model candidate needs ~15 GB.')
""")

md(r"""
## 5. Dependencies
apt + pinned pip packages (`requirements/colab.txt`). pip downloads are cached on Drive; a marker
file skips reinstalling within the same VM. Colab's own torch/transformers are kept as-is.
""")
code(r"""
import hashlib, importlib, subprocess, sys
from pathlib import Path
req = CODE / 'requirements' / 'colab.txt'
marker = Path('/content/runtime') / ('.deps_' + hashlib.sha1(req.read_bytes()).hexdigest()[:12])
if sys.version_info >= (3, 13):
    print(f'NOTE: Python {sys.version.split()[0]}: the kokoro package needs < 3.13, so it will be skipped '
          '(English TTS then falls back to the next available engine). See docs.')
    reqs = [l for l in req.read_text().splitlines() if not l.startswith(('kokoro', 'misaki'))]
    req = Path('/content/runtime/tmp/requirements.txt'); req.write_text('\n'.join(reqs))
if marker.exists():
    print('dependencies already installed in this VM')
else:
    subprocess.run('apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq espeak-ng ffmpeg rsync lsb-release > /dev/null',
                   shell=True, check=True)
    env = dict(os.environ, PIP_CACHE_DIR=str(DRIVE / 'cache' / 'pip'))
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(req)], check=True, env=env)
    marker.touch()

versions = {}
for mod in ['faster_whisper', 'ctranslate2', 'silero_vad', 'soundfile', 'jiwer', 'aiohttp', 'yaml',
            'transformers', 'huggingface_hub', 'torch', 'kokoro']:
    try:
        m = importlib.import_module(mod)
        versions[mod] = getattr(m, '__version__', 'ok')
    except Exception as e:
        versions[mod] = f'MISSING ({type(e).__name__}: {e})'
for k, v in versions.items():
    print(f'{k:16} {v}')
import ctranslate2
print('ctranslate2 CUDA devices:', ctranslate2.get_cuda_device_count())
from ops.env import nvidia_lib_dirs
print('CUDA wheel lib dirs passed to child processes:', len(nvidia_lib_dirs()))
""")

md(r"""
## 6. Environment configuration and secret handling

Preferred: add secrets in Colab's **Secrets** panel (key icon, left sidebar) and grant this notebook access.
Anything not found there is generated once (strong random values) and stored in
`MyDrive/voice-agent/secrets/generated.env`. Values are exported to the environment and **never printed**.

| Secret | Purpose |
|---|---|
| `VOICE_ENGINE_WS_TOKEN` | browser/WebSocket access to the voice engine |
| `VOICE_ENGINE_GATEWAY_TOKEN` | Redmi 9C HFP gateway box → voice engine (copy into the gateway's env file) |
| `VOICE_ENGINE_CALLS_TOKEN` | n8n → `POST /calls` (outbound calls through the Redmi 9C) |
| `LLM_API_KEY` | llama-server bearer key (local only, defence in depth) |
| `PG_*_PASSWORD` | PostgreSQL roles `postgres`, `va_owner`, `va_app`, `n8n_sys` |
| `N8N_ENCRYPTION_KEY` | encrypts credentials stored by n8n. **Losing it makes stored n8n credentials unrecoverable.** |
| `N8N_WEBHOOK_HMAC_SECRET` | voice engine → n8n webhook signatures |
| optional: `HF_TOKEN`, `AZURE_SPEECH_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`, `GIT_REPO_URL`, `GIT_REPO_TOKEN` | |
""")
code(r"""
from ops import secrets as ops_secrets
sources = ops_secrets.load_all()
for name, src in sources.items():
    print(f'{name:28} {src:16} {ops_secrets.mask(os.environ.get(name, ""))}')
from ops.env import child_env
_e = child_env()
print('\nVOICE_AGENT_ROOT =', _e['VOICE_AGENT_ROOT'])
print('HF_HOME          =', _e['HF_HOME'])
""")

md(r"""
## 7. Rappelo repository: clone and inspect (Phase 1 re-check)
Clones into Drive and re-runs the static audit. The verdict (no unrooted two-way call audio) is in
`docs/phase1-rappelo-audit.md`; this cell lets you confirm it from Colab.
""", extra=True)
code(r"""
from ops.run import py
vendor = DRIVE / 'vendor' / 'rappelo'
subprocess.run([sys.executable, str(CODE / 'tools' / 'audit_rappelo.py'), '--repo', str(vendor), '--clone',
                '--json', str(DRIVE / 'results' / 'rappelo_audit.json')], check=True)
""", extra=True)

md(r"""
## 8. Android build environment: deliberately not installed

The Redmi 9C is the telephony endpoint, but it needs **no app**. An unrooted Android 10 app cannot read or
inject cellular call audio. Android does send full two-way call audio to a paired **Bluetooth hands-free
device**, so the phone is connected over Bluetooth HFP to a small Linux box running `gateway/`
(oFono + PipeWire), which streams the call to this voice engine (see `docs/telephony-redmi9c-gateway.md`).
An Android SDK/Gradle toolchain (~6 GB, ~15 min per session) would build nothing we use.
It can't run in Colab anyway: Colab has no Bluetooth and isn't next to your phone.
""", extra=True)

md(r"""
## 9. PostgreSQL 16
Data dir on local disk (`/content/runtime/pgdata`), listening on 127.0.0.1 only. On a fresh VM the
latest Drive backup is restored automatically. A background service dumps all databases to Drive
every 10 minutes (keep 48). **Data written after the last backup is lost if the VM is recycled.**
""")
code(r"""
from ops import pg
from ops.services import Service
pg.install()
fresh = pg.init_cluster()
pg_svc = pg.service()
pg_svc.start(wait_s=60)
pg.bootstrap_roles()
if fresh:
    print('restore:', pg.restore_latest())
print(pg.psql('select version()'))
from ops import migrate
migrate.migrate(*migrate.colab_conn())      # tables + demo business; already-applied files are skipped
backup_svc = Service('pg-backup', [sys.executable, '-m', 'ops.pg', 'backup-loop', '600'], cwd=str(CODE))
backup_svc.start()
""")

md(r"""
## 10. Model downloads (persistent on Drive)
Choose candidates. Defaults download what the benchmarks in section 16 compare.
Licences are printed. **Qwen2.5-3B (research licence) and MMS-TTS (CC-BY-NC) are evaluation baselines only.**
""", extra=True)
code(r"""
STT_MODELS = ['whisper-small', 'whisper-medium', 'whisper-large-v3-turbo']   #@param
LLM_MODELS = ['qwen2.5-3b', 'qwen3-4b', 'gemma3-4b', 'qwen2.5-7b', 'qwen3-8b']  #@param
TTS_MODELS = ['kokoro', 'mms-urd']                                             #@param
from ops import models as ops_models
os.environ['HF_HOME'] = str(DRIVE / 'cache' / 'hf')
ops_models.download('stt', STT_MODELS)
ops_models.download('tts', TTS_MODELS)
ops_models.download('llm', LLM_MODELS)
""", extra=True)

md(r"""
## 11. Active model configuration
Selects the models the voice engine uses. Values become `VE__*` overrides of `config/voice_engine.yaml`.
Pick these from the benchmark report (section 16); the defaults below are starting points, not conclusions.
""")
code(r"""
ACTIVE_STT = 'whisper-large-v3-turbo'   #@param ['whisper-small', 'whisper-medium', 'whisper-large-v3-turbo']
ACTIVE_LLM = 'qwen3-8b'                 #@param ['qwen3-8b', 'qwen2.5-7b', 'qwen3-4b', 'gemma3-4b', 'qwen2.5-3b']
TTS_EN = 'kokoro'                       #@param ['kokoro', 'mms', 'azure']
TTS_UR = 'mms'                          #@param ['mms', 'azure']
AZURE_REGION = ''                       #@param {type:'string'}
BUSINESS_DATA = 'n8n'                   #@param ['n8n', 'static']
from ops import models as ops_models
os.environ['HF_HOME'] = str(DRIVE / 'cache' / 'hf')
if TTS_EN == 'kokoro' and not ops_env.python_ok_for_kokoro():
    TTS_EN = 'azure' if os.environ.get('AZURE_SPEECH_KEY') else 'mms'
    print(f'kokoro needs Python < 3.13 (this runtime: {sys.version.split()[0]}); English TTS ->', TTS_EN)
# download only what the agent uses (already-downloaded models are skipped)
ops_models.download('stt', [ACTIVE_STT])
ops_models.download('llm', [ACTIVE_LLM])
ops_models.download('tts', [t for t, used in (('kokoro', TTS_EN == 'kokoro'), ('mms-eng', TTS_EN == 'mms'),
                                               ('mms-urd', TTS_UR == 'mms')) if used])
cat = ops_models.catalog()
stt_dir = {m['id']: m['dir'] for m in cat['stt']}[ACTIVE_STT]
llm_meta = {m['id']: m for m in cat['llm']}[ACTIVE_LLM]
import yaml
override = yaml.safe_load((CODE / 'config' / 'voice_engine.yaml').read_text(encoding='utf-8'))
override['stt']['model_path'] = f'models/{stt_dir}'
override['tts']['engine_by_language'] = {'en': TTS_EN, 'ur': TTS_UR}
override['tts']['azure_region'] = AZURE_REGION
override['llm']['chat_template_kwargs'] = llm_meta.get('chat_template_kwargs') or {}
# 'n8n' = real menu/orders/bookings from PostgreSQL via the n8n workflows (cell 10 installs them);
# 'static' = the old fake test data in config/dev_business.yaml
override['business']['gateway'] = BUSINESS_DATA
override['business']['n8n_base_url'] = 'http://127.0.0.1:5678/webhook'
RUNTIME_CFG = Path('/content/runtime/voice_engine.runtime.yaml')
RUNTIME_CFG.write_text(yaml.safe_dump(override, allow_unicode=True, sort_keys=False), encoding='utf-8')
os.environ['VOICE_ENGINE_CONFIG'] = str(RUNTIME_CFG)   # benchmarks and child processes read this
print('runtime config ->', RUNTIME_CFG)
print({'stt': stt_dir, 'llm': llm_meta['file'], 'tts': override['tts']['engine_by_language']})
""")

md(r"""
## 12. LLM server (llama.cpp `llama-server`, CUDA)
Downloads llama.cpp's official prebuilt CUDA build (~730 MB, about 1–2 min per new VM) and checks that it sees
the GPU. Only if that fails does it compile from source (30–60 min on Colab's 2 CPUs, then cached on Drive).
Serves the active GGUF on 127.0.0.1:8081 with an API key. Restarting with another model is how section 16
benchmarks each LLM.
""")
code(r"""
from ops import llm_server
import json
llm_server.ensure_built()
print(llm_server.version()[-300:])

def start_llm(model_id, ctx=8192, parallel=2):
    meta = {m['id']: m for m in ops_models.catalog()['llm']}[model_id]
    svc = llm_server.service(ops_models.gguf_path(model_id), ctx=ctx, parallel=parallel,
                             template_kwargs=json.dumps(meta['chat_template_kwargs']) if meta.get('chat_template_kwargs') else None)
    svc.stop()
    svc.start(wait_s=300)
    return svc

llm_svc = start_llm(ACTIVE_LLM)
""")

md(r"""
## 13. n8n (business logic engine) on PostgreSQL
Installed from npm once and cached on Drive. Bound to 127.0.0.1; opened through Colab's authenticated
proxy, never through the public tunnel. On first open, create the n8n owner account (use a strong password).
Workflows arrive in Phase 6.
""")
code(r"""
from ops import n8n as ops_n8n
ops_n8n.ensure_node()
ops_n8n.ensure_n8n()
n8n_svc = ops_n8n.service()
n8n_svc.stop()                      # the CLI import below must run while n8n is stopped
ops_n8n.install_workflows()         # credentials (encrypted by n8n) + 7 workflows, published
n8n_svc.start(wait_s=240)
from ops.run import py
py('ops.n8n_check', check=False)    # end-to-end check against a temporary test business (PASS/FAIL per step)
from google.colab import output as colab_output
colab_output.serve_kernel_port_as_window(ops_n8n.PORT, anchor_text='Open n8n editor (Colab-authenticated)')
""")

md(r"""
## 14. Health checks
""")
code(r"""
from ops import health
_ = health.print_report()
""")

md(r"""
## 15. Automated tests (no GPU models needed)
Unit/integration tests for the voice engine logic: audio, text, VAD endpointing, STT language policy,
LLM streaming + tool-call parsing, n8n HMAC signing and retries, session turn-taking, barge-in, WebSocket auth.
""", extra=True)
code(r"""
rc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider'], cwd=CODE, env=child_env()).returncode
print('pytest exit code', rc, '(0 = all passed)')
""", extra=True)

md(r"""
## 16. Phase 4 benchmarks (T4)

### 16a. Test datasets (existing public data, nothing to record)

| Set | What it is | Group in results | Licence |
|---|---|---|---|
| FLEURS `ur_pk`, `en_us` (test split) | read sentences, clean studio-like audio | `fleurs_ur`, `fleurs_en` | CC-BY-4.0 |
| UrduSpeech benchmark **CS** | real **Urdu-English code-switched** speech (podcasts, interviews, vlogs, dramas) | `us_mixed` | CC-BY-4.0 |
| UrduSpeech benchmark **Std** | real conversational / broadcast Pakistani Urdu | `us_urdu` | CC-BY-4.0 |
| UrduSpeech benchmark **EngPk** | Pakistani-accented English | `us_english_pk` | CC-BY-4.0 |

UrduSpeech (ASLP-lab, 2026, arXiv:2605.17846): only the **manually corrected benchmark** split is used, short
clips (≤10 s), sampled evenly across content categories. Its code-switched transcripts keep many English words in
Latin script ("image", "I also found") and write others in Urdu script, so the STT report adds two script-aware
scores next to WER: `wer_urdu_tokens` (Urdu-script words only) and `latin_word_recall` (English words recognised
in Latin script).
""", extra=True)
code(r"""
N_PER_SET = 60   #@param {type:'integer'}
py('bench.datasets', 'fleurs', '--lang', 'ur_pk', '--n', str(N_PER_SET))
py('bench.datasets', 'fleurs', '--lang', 'en_us', '--n', str(N_PER_SET))
for subset in ['CS', 'Std', 'EngPk']:
    py('bench.datasets', 'urduspeech', '--subset', subset, '--n', str(N_PER_SET))
MANIFESTS = ['datasets/manifest_fleurs_ur_pk.jsonl', 'datasets/manifest_fleurs_en_us.jsonl',
             'datasets/manifest_urduspeech_CS.jsonl', 'datasets/manifest_urduspeech_Std.jsonl',
             'datasets/manifest_urduspeech_EngPk.jsonl']
print(MANIFESTS)
""", extra=True)

md(r"""
### 16c. STT: WER/CER, keyword recall, language ID, latency, VRAM
Wideband (16 kHz) and simulated phone channel (8 kHz G.711). Models load one at a time.
""", extra=True)
code(r"""
py('bench.stt_bench', '--models', ','.join(STT_MODELS), '--manifests', ','.join(MANIFESTS), '--conditions', 'wide,phone')
""", extra=True)

md(r"""
### 16d. LLM: tool calling, reply language/script, TTFT, decode speed
Restarts `llama-server` once per candidate. Reply *content* is saved for your review, not auto-scored.
""", extra=True)
code(r"""
for mid in LLM_MODELS:
    start_llm(mid)
    py('bench.llm_bench', '--model-id', mid, '--repeats', '2', check=False)
llm_svc = start_llm(ACTIVE_LLM)
""", extra=True)

md(r"""
### 16e. TTS: latency, real-time factor, VRAM + listening sheet
Fill the 1–5 scores in `results/phase4/tts_listening_sheet.csv` by ear (WAVs are in `results/phase4/tts_audio/`).
""", extra=True)
code(r"""
engines = ['kokoro', 'mms'] + (['azure'] if os.environ.get('AZURE_SPEECH_KEY') and AZURE_REGION else [])
py('bench.tts_bench', '--engines', ','.join(engines), check=False,
   extra_env={'VE__TTS__AZURE_REGION': AZURE_REGION} if AZURE_REGION else None)
""", extra=True)

md(r"""
### 16f. VAD: CPU cost, detection, false triggers on noise
""", extra=True)
code(r"""
py('bench.vad_bench', '--manifests', ','.join(MANIFESTS))
""", extra=True)

md(r"""
### 16g. Can STT + TTS + LLM share the T4 concurrently?
Loads the active STT/TTS in one process alongside `llama-server` and compares each stage alone vs. overlapped.
This is the measurement behind the sequential-vs-concurrent decision in the docs.
""", extra=True)
code(r"""
py('bench.concurrency', '--manifest', MANIFESTS[0], '--iters', '8')
""", extra=True)

md(r"""
### 16h. Report
""", extra=True)
code(r"""
py('bench.report')
""", extra=True)

md(r"""
## 17. Voice engine + public test URL (browser microphone over WebSocket)
Starts the engine on 127.0.0.1:8765 and a Cloudflare quick tunnel to it. Open the printed URL in Chrome,
paste `VOICE_ENGINE_WS_TOKEN` (from the Colab Secrets panel or `secrets/generated.env` on Drive; it is not
printed here), click **Start**, and talk. **Use headphones.** The business side is the dev fixture
(`config/dev_business.yaml`); n8n replaces it in Phase 7.
""")
code(r"""
from ops import tunnel
from ops.services import http_ok
ve_svc = Service('voice-engine', [sys.executable, '-m', 'voice_engine', 'serve', '--config', str(RUNTIME_CFG)],
                 cwd=str(CODE), health=lambda: http_ok('http://127.0.0.1:8765/healthz'))
ve_svc.start(wait_s=600)
tunnel.ensure_cloudflared()
tun_svc = tunnel.service()
tun_svc.stop()                      # restart so the current settings apply (the URL changes)
tun_svc.start()
PUBLIC_URL = tunnel.public_url(tun_svc)      # waits until the tunnel is really connected
if PUBLIC_URL:
    import time
    reachable = False
    for _ in range(12):                      # new hostnames can take up to a minute to resolve
        reachable = http_ok(PUBLIC_URL + '/healthz', timeout=10)
        if reachable:
            break
        time.sleep(5)
    print('Test client:', PUBLIC_URL)
    print('public /healthz reachable:', reachable)
    print('Token: VOICE_ENGINE_WS_TOKEN from the Colab Secrets panel or Drive voice-agent/secrets/generated.env')
""")

md(r"""
### 17a. Phone line status + manual outbound test call (Redmi 9C)
Shows whether the gateway box is connected, then places a call through the same `POST /calls` API that the
n8n Outbound Call Scheduler will use (Phase 6). **This calls a real number from your Ufone SIM.** The gateway
only dials numbers in its own `outbound_allowlist`, whatever is sent here.
""")
code(r"""
import json, urllib.request
TEST_NUMBER = ''   #@param {type:'string'}
h = json.loads(urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5).read() or b'{}') if http_ok('http://127.0.0.1:8765/healthz') else {}
print('telephony:', h.get('telephony', 'voice engine not healthy'))
if TEST_NUMBER:
    req = urllib.request.Request('http://127.0.0.1:8765/calls', method='POST',
        data=json.dumps({'number': TEST_NUMBER, 'context': {'purpose': 'manual_test'}}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['VOICE_ENGINE_CALLS_TOKEN']})
    try:
        call = json.loads(urllib.request.urlopen(req, timeout=10).read())
        print('call requested:', call)
    except urllib.error.HTTPError as e:
        print('refused:', e.code, e.read().decode()[:200])
""")
code(r"""
# Follow the call (re-run while it progresses). Statuses: requested dialing alerting active ended | failed rejected
if TEST_NUMBER and 'call' in globals():
    req = urllib.request.Request(f"http://127.0.0.1:8765/calls/{call['call_id']}",
                                 headers={'Authorization': 'Bearer ' + os.environ['VOICE_ENGINE_CALLS_TOKEN']})
    print(json.loads(urllib.request.urlopen(req, timeout=5).read()))
""")

md(r"""
### 17b. End-to-end self-test (no browser)
Feeds a recorded WAV through VAD → STT → LLM → TTS with the same code as live sessions and writes the
agent's audio reply. Uses your first recording if present, otherwise a FLEURS English clip.
**Stops the live engine first** so two copies of the models do not compete for VRAM.
""", extra=True)
code(r"""
ve_svc.stop()
own_wavs = sorted((DRIVE / 'datasets' / 'own').glob('*.wav'))
src = own_wavs[0] if own_wavs else sorted((DRIVE / 'datasets' / 'fleurs' / 'en_us').glob('*.wav'))[0]
out = DRIVE / 'results' / 'phase4' / 'selftest_reply.wav'
py('voice_engine', 'selftest', '--config', str(RUNTIME_CFG), '--wav', str(src), '--out', str(out), check=False)
from IPython.display import Audio, display
if out.exists():
    display(Audio(str(out)))
ve_svc.start(wait_s=600)
""", extra=True)

md(r"""
## 18. Logs
Service logs persist on Drive (`logs/*.log`). Per-turn latency records: `logs/voice_engine/turns.jsonl`.
""")
code(r"""
for name in ['postgres', 'pg-backup', 'llm', 'n8n', 'voice-engine', 'tunnel']:
    s = Service(name, [])
    print(f'===== {name} ({"running" if s.running() else "stopped"}) =====')
    print(s.tail(8))
turns = DRIVE / 'logs' / 'voice_engine' / 'turns.jsonl'
if turns.exists():
    import json, statistics
    rows = [json.loads(l) for l in turns.read_text(encoding='utf-8').splitlines() if l.strip()]
    for k in ['stt_s', 'llm_ttft_s', 'first_sentence_s', 'first_audio_s']:
        v = [r[k] for r in rows if r.get(k) is not None]
        if v:
            print(f'{k:18} n={len(v):4} mean={statistics.mean(v):.3f}s  max={max(v):.3f}s')
""")

md(r"""
## 19. Diagnostics
""")
code(r"""
print(subprocess.run('nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv', shell=True, capture_output=True, text=True).stdout)
print(subprocess.run('ss -ltnp | grep -E ":(5432|5678|8081|8765)\\b" || true', shell=True, capture_output=True, text=True).stdout)
print(subprocess.run('free -h; df -h /content | tail -1', shell=True, capture_output=True, text=True).stdout)
for name in ['voice-engine', 'llm', 'n8n']:
    errs = [l for l in Service(name, []).tail(400).splitlines() if 'ERROR' in l or 'Traceback' in l]
    print(f'{name}: {len(errs)} error lines in last 400', *errs[-5:], sep='\n  ')
""")

md(r"""
## 20. Shutdown and recovery

Run before closing the tab: final database backup, then services stop in dependency order.

**After a disconnect / runtime reset** everything under `/content` is gone. Re-run cells **1 → 14**:
code re-sync, deps (cached wheels), secrets (from Colab Secrets / Drive), PostgreSQL (fresh cluster +
automatic restore of the latest backup), llama-server (cached binary), n8n (cached install). Then 17 for the
voice engine; the quick-tunnel URL will be **different** each time.
**After a disconnect without reset** (VM kept): just re-run 1, 2, 6 and 14; services kept running.
""")
code(r"""
try:
    pg.backup()
except Exception as e:                      # still stop everything; report the backup problem
    print('BACKUP FAILED:', e)
for name in ['tunnel', 'voice-engine', 'n8n', 'llm', 'pg-backup']:
    Service(name, []).stop()
pg.service().stop()
print('all services stopped; backups are in', DRIVE / 'backups' / 'pg')
""")


# --------------------------------------------------------------------------------------------
def _python_body(src: str) -> str:
    """Strip IPython-only syntax so the cell can be checked with ast.parse."""
    lines = []
    for line in src.splitlines():
        s = line.lstrip()
        lines.append("" if s.startswith(("!", "%")) else re.sub(r"#@param.*$", "", line))
    return "\n".join(lines)


def build() -> Path:
    nb = new_notebook()
    nb.metadata.update({
        "colab": {"provenance": [], "gpuType": "T4", "toc_visible": True},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
        "accelerator": "GPU",
    })
    for kind, src, extra in CELLS:
        if (kind == "md" and not INCLUDE_TEXT) or (extra and not FULL):
            continue
        nb.cells.append(new_markdown_cell(src) if kind == "md" else new_code_cell(src))
    nbformat.validate(nb)
    errors = []
    for i, (kind, src, _extra) in enumerate(CELLS):
        if kind == "code":
            try:
                ast.parse(_python_body(src))
            except SyntaxError as e:
                errors.append(f"cell {i}: {e}")
    if errors:
        raise SystemExit("syntax errors:\n" + "\n".join(errors))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, OUT)
    return OUT


if __name__ == "__main__":
    p = build()
    nb_cells = nbformat.read(p, as_version=4).cells
    print(f"wrote {p} ({len(nb_cells)} cells, {sum(c.cell_type == 'code' for c in nb_cells)} code), "
          "nbformat-valid, all code cells parse")
    sys.exit(0)
