#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# Force Asia/Shanghai so datetime.now() / isoformat() write local wall-clock
# time into job.json `updated_at` (and any other persisted timestamps).
# The base image is python:3.11-slim on UTC, but the host is CST and
# every consumer of these timestamps assumes local time.
os.environ.setdefault('TZ', 'Asia/Shanghai')
try:
    time.tzset()
except Exception:
    # tzset() can fail on platforms without proper zoneinfo; fall back silently.
    pass
from urllib.parse import urlparse, unquote
from flask import Flask, request, jsonify, render_template, send_from_directory, session, make_response

# ── 配置加载 ──────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
TOPIC_RECOMMENDATIONS_PATH = os.path.join(os.path.dirname(__file__), 'topic_recommendations.json')

DEFAULT_CONF = {
    'password': '',
    'secret_key': '',
    'port': 9999,
    'download_root': '/root/.openclaw/workspace/public-downloads',
    'cosmic_sleep_folder': 'cosmic-sleep',
    'local_artifact_retention_hours': 72,
}

CONF = DEFAULT_CONF.copy()
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as f:
        CONF.update(json.load(f))

ENV_MAP = {
    'VOICE_STUDIO_PASSWORD': 'password',
    'VOICE_STUDIO_SECRET_KEY': 'secret_key',
    'VOICE_STUDIO_PORT': 'port',
    'VOICE_STUDIO_DOWNLOAD_ROOT': 'download_root',
    'VOICE_STUDIO_COSMIC_FOLDER': 'cosmic_sleep_folder',
    'VOICE_STUDIO_LOCAL_ARTIFACT_RETENTION_HOURS': 'local_artifact_retention_hours',
}
for env_key, conf_key in ENV_MAP.items():
    if os.getenv(env_key):
        CONF[conf_key] = os.getenv(env_key)

CONF['port'] = int(CONF.get('port') or 9999)
CONF['local_artifact_retention_hours'] = max(
    1, float(CONF.get('local_artifact_retention_hours') or 72)
)

app = Flask(__name__, template_folder='templates')
PASSWORD = CONF.get('password') or ''
if not PASSWORD:
    raise RuntimeError('Set VOICE_STUDIO_PASSWORD or create config.json with a password.')
app.secret_key = CONF.get('secret_key') or PASSWORD
PORT = CONF.get('port', 9999)
DOWNLOAD_ROOT = CONF.get('download_root', '/root/.openclaw/workspace/public-downloads')
COSMIC_FOLDER = CONF.get('cosmic_sleep_folder', 'cosmic-sleep')
COSMIC_DIR = os.path.join(DOWNLOAD_ROOT, COSMIC_FOLDER)
SKILL_DIR = Path(__file__).resolve().parent
RUNS_DIR = SKILL_DIR / 'runs'
RUNS_DIR.mkdir(exist_ok=True)
LOCAL_ARTIFACT_RETENTION_SECONDS = CONF['local_artifact_retention_hours'] * 3600
LOCAL_ARTIFACT_CLEANUP_INTERVAL_SECONDS = 6 * 3600
_local_artifact_cleanup_lock = threading.Lock()
_last_local_artifact_cleanup = 0.0
BGM_DIR = SKILL_DIR / 'bgm'
BGM_DIR.mkdir(exist_ok=True)

# ── 模块配置（可扩展，新增 video 只需在这里加一条）─────────────────────────
MODE_CONFIG = {
    'voice': {
        'name': '声音',
        'icon': '🎤',
        'job_dir': os.path.join(os.path.dirname(__file__), 'jobs', 'voice'),
        'archive_dir': os.path.join(os.path.dirname(__file__), 'archive', 'voice'),
    },
    'music': {
        'name': '音乐',
        'icon': '🎵',
        'job_dir': os.path.join(os.path.dirname(__file__), 'jobs', 'music'),
        'archive_dir': os.path.join(os.path.dirname(__file__), 'archive', 'music'),
    },
    'cover': {
        'name': '翻唱',
        'icon': '🎤',
        'job_dir': os.path.join(os.path.dirname(__file__), 'jobs', 'cover'),
        'archive_dir': os.path.join(os.path.dirname(__file__), 'archive', 'cover'),
    },
}

for cfg in MODE_CONFIG.values():
    os.makedirs(cfg['job_dir'], exist_ok=True)
    os.makedirs(cfg['archive_dir'], exist_ok=True)

# 旧目录迁移（向后兼容）
LEGACY_JOBS_DIR = os.path.join(os.path.dirname(__file__), 'jobs')
for fname in os.listdir(LEGACY_JOBS_DIR):
    if fname.endswith('.json') and os.path.isfile(os.path.join(LEGACY_JOBS_DIR, fname)):
        src = os.path.join(LEGACY_JOBS_DIR, fname)
        dst = os.path.join(MODE_CONFIG['voice']['job_dir'], fname)
        if not os.path.exists(dst):
            shutil.move(src, dst)

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), 'archive')
os.makedirs(ARCHIVE_DIR, exist_ok=True)

def _trigger_writer():
    """Touch a host-watched trigger file.

    The Flask app runs inside Docker and must not call `openclaw agent` there:
    the container cannot reach the host gateway/auth context reliably. A
    systemd path unit on the host watches this file and runs the writer.
    """
    try:
        (SKILL_DIR / '.writer-trigger').write_text(str(time.time()), encoding='utf-8')
    except OSError as e:
        print(f'[writer] failed to touch trigger: {e}')

def get_mode_cfg(mode):
    """返回 mode 对应的配置，未知 mode 回退 voice"""
    return MODE_CONFIG.get(mode, MODE_CONFIG['voice'])

def job_dir(mode):
    return get_mode_cfg(mode)['job_dir']

def job_archive_dir(mode):
    return get_mode_cfg(mode)['archive_dir']

def job_path(job_id, mode=None):
    if mode is None:
        # 兼容：遍历所有已注册 mode 找文件
        for m, cfg in MODE_CONFIG.items():
            p = os.path.join(cfg['job_dir'], f'{job_id}.json')
            if os.path.exists(p):
                return p
        return os.path.join(MODE_CONFIG['voice']['job_dir'], f'{job_id}.json')
    # music_cover jobs store under MODE_CONFIG['cover'] (job['mode'] stays 'music_cover')
    if mode == 'music_cover':
        mode = 'cover'
    return os.path.join(job_dir(mode), f'{job_id}.json')

def save_job(job):
    job['updated_at'] = datetime.now().isoformat()
    mode = job.get('mode', 'voice')
    with open(job_path(job['id'], mode), 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False, indent=2)

def load_job(job_id, mode=None):
    p = job_path(job_id, mode)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)

def load_topic_recommendations():
    if not os.path.exists(TOPIC_RECOMMENDATIONS_PATH):
        return []
    with open(TOPIC_RECOMMENDATIONS_PATH, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    topics = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get('title') or '').strip()
        if not title:
            continue
        topics.append({
            'title': title,
            'category': (item.get('category') or '其他').strip(),
            'angle': (item.get('angle') or '').strip(),
            'source': (item.get('source') or '').strip(),
            'source_url': (item.get('source_url') or '').strip(),
            'updated_at': (item.get('updated_at') or '').strip(),
            'evergreen': bool(item.get('evergreen', False)),
        })
    return topics

def job_response(job):
    """Return a UI-friendly copy with legacy voice fields normalized."""
    if not job:
        return job
    data = dict(job)
    if data.get('mode') in ('theme', 'script'):
        runs = list(data.get('voice_runs') or [])
        if not runs and (data.get('voice_url') or data.get('final_url') or data.get('audio_url')):
            runs.append({
                'run_id': 'legacy',
                'provider': data.get('provider') or 'azure',
                'voice': data.get('voice') or 'zh-CN-YunzeNeural',
                'bgm': bool(data.get('bgm', True)),
                'bgm_asset': data.get('bgm_asset') or 'bgm_default.mp3',
                'script_url': data.get('script_url'),
                'voice_url': data.get('voice_url') or data.get('audio_url'),
                'final_url': data.get('final_url') or data.get('voice_url') or data.get('audio_url'),
                'created_at': data.get('updated_at') or data.get('created_at'),
                'status': data.get('status') or 'done',
            })
        if runs:
            data['voice_runs'] = runs
            latest = runs[-1]
            if not data.get('voice_url'):
                data['voice_url'] = latest.get('voice_url')
            if not data.get('final_url'):
                data['final_url'] = latest.get('final_url') or latest.get('voice_url')
            if not data.get('script_url'):
                data['script_url'] = latest.get('script_url')
    # Music mode: ensure music_runs is always a list (oldest first, current last)
    if data.get('mode') == 'music':
        runs = list(data.get('music_runs') or [])
        # If empty, synthesize one legacy entry from current audio_url/lyrics
        if not runs and (data.get('audio_url') or data.get('audio_path')):
            runs.append({
                'run_id': 'legacy',
                'audio_url': data.get('audio_url'),
                'lyrics_url': data.get('lyrics_url'),
                'lyrics': data.get('lyrics') or data.get('edited_lyrics'),
                'audio_duration_ms': data.get('audio_duration_ms'),
                'created_at': data.get('updated_at') or data.get('created_at'),
                'is_current': True,
                'label': '初始版本',
            })
        data['music_runs'] = runs
    return data

def archive_job(job):
    """完成后归档，保留记录"""
    mode = job.get('mode', 'voice')
    if mode == 'music_cover':
        mode = 'cover'
    dest_dir = job_archive_dir(mode)
    dest = os.path.join(dest_dir, f'{job["id"]}.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.remove(job_path(job['id'], mode))

def is_relative_to(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False

def safe_unlink(path, allowed_roots):
    if not path:
        return
    target = Path(path)
    if not target.exists() or not target.is_file():
        return
    if any(is_relative_to(target, root) for root in allowed_roots):
        target.unlink()

def _unlink_expired_file(path, cutoff):
    """Delete one uploaded local artifact after its retention window."""
    target = Path(path)
    try:
        if (
            target.is_file()
            and is_relative_to(target, RUNS_DIR)
            and target.stat().st_mtime <= cutoff
        ):
            size = target.stat().st_size
            target.unlink()
            return size
    except FileNotFoundError:
        pass
    return 0

def _remove_uploaded_chunk_dir(path):
    """Remove Azure chunks once both voice and final artifacts are uploaded."""
    chunk_dir = Path(path)
    try:
        if (
            chunk_dir.name == 'azure_chunks'
            and chunk_dir.is_dir()
            and is_relative_to(chunk_dir, RUNS_DIR)
        ):
            files = [item for item in chunk_dir.rglob('*') if item.is_file()]
            size = sum(item.stat().st_size for item in files)
            count = len(files)
            shutil.rmtree(chunk_dir)
            return count, size
    except FileNotFoundError:
        pass
    return 0, 0

def _iter_persisted_jobs():
    """Yield active and archived jobs, ignoring malformed records."""
    roots = []
    for cfg in MODE_CONFIG.values():
        roots.extend((Path(cfg['job_dir']), Path(cfg['archive_dir'])))

    seen_paths = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob('*.json'):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                with path.open(encoding='utf-8') as f:
                    job = json.load(f)
                if isinstance(job, dict) and job.get('id'):
                    yield job
            except (OSError, json.JSONDecodeError):
                continue

def cleanup_expired_local_artifacts(now=None):
    """Remove uploaded audio artifacts older than the configured retention."""
    now = time.time() if now is None else now
    cutoff = now - LOCAL_ARTIFACT_RETENTION_SECONDS
    deleted_files = 0
    deleted_bytes = 0

    def remove(path):
        nonlocal deleted_files, deleted_bytes
        size = _unlink_expired_file(path, cutoff)
        if size:
            deleted_files += 1
            deleted_bytes += size

    for job in _iter_persisted_jobs():
        base = RUNS_DIR / str(job['id'])
        mode = job.get('mode')

        if mode in ('theme', 'script'):
            for run in job.get('voice_runs') or []:
                run_id = run.get('run_id')
                if not run_id or run_id == 'legacy':
                    continue
                run_dir = base / run_id
                if run.get('voice_url'):
                    remove(run_dir / 'voice.mp3')
                if run.get('final_url') and run.get('bgm'):
                    remove(run_dir / 'mixed.mp3')
                if run.get('voice_url') and run.get('final_url'):
                    chunk_files, chunk_bytes = _remove_uploaded_chunk_dir(
                        run_dir / 'azure_chunks'
                    )
                    deleted_files += chunk_files
                    deleted_bytes += chunk_bytes

        elif mode == 'music':
            if job.get('audio_url'):
                remove(base / 'music.mp3')
                remove(base / 'music.320k.mp3')

        elif mode == 'music_cover':
            if job.get('audio_url'):
                remove(base / 'cover.mp3')
            if job.get('reference_audio_url'):
                for reference in base.glob('reference.*'):
                    remove(reference)

    return {'files': deleted_files, 'bytes': deleted_bytes}

def maybe_cleanup_expired_local_artifacts(force=False):
    """Rate-limit cleanup so normal requests do not repeatedly scan the disk."""
    global _last_local_artifact_cleanup
    now_monotonic = time.monotonic()
    if (
        not force
        and now_monotonic - _last_local_artifact_cleanup
        < LOCAL_ARTIFACT_CLEANUP_INTERVAL_SECONDS
    ):
        return None
    if not _local_artifact_cleanup_lock.acquire(blocking=False):
        return None
    try:
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - _last_local_artifact_cleanup
            < LOCAL_ARTIFACT_CLEANUP_INTERVAL_SECONDS
        ):
            return None
        result = cleanup_expired_local_artifacts()
        _last_local_artifact_cleanup = now_monotonic
        if result['files']:
            print(
                '[cleanup] removed '
                f"{result['files']} uploaded local audio files "
                f"({result['bytes']} bytes)"
            )
        return result
    finally:
        _local_artifact_cleanup_lock.release()

def public_path_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    path = unquote(parsed.path or '')
    prefix = f'/{COSMIC_FOLDER}/'
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    if not name or '/' in name:
        return None
    return Path(DOWNLOAD_ROOT) / COSMIC_FOLDER / name

def cleanup_job_outputs(job):
    """Remove stale generated files before retrying a job."""
    allowed_roots = [RUNS_DIR, DOWNLOAD_ROOT]
    for key in ('audio_path', 'final_path'):
        safe_unlink(job.get(key), allowed_roots)
    for key in ('audio_url', 'final_url'):
        safe_unlink(public_path_from_url(job.get(key)), allowed_roots)

    run_dir = RUNS_DIR / job['id']
    if run_dir.exists() and run_dir.is_dir() and is_relative_to(run_dir, RUNS_DIR):
        shutil.rmtree(run_dir)

def delete_job(job):
    """Delete a job from the active UI and clean generated local outputs."""
    mode = job.get('mode', 'voice')
    if mode == 'music_cover':
        mode = 'cover'
    cleanup_job_outputs(job)
    source = Path(job_path(job['id'], mode))
    if not source.exists():
        return
    deleted_dir = Path(job_archive_dir(mode)) / 'deleted'
    deleted_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    shutil.move(str(source), str(deleted_dir / f'{stamp}-{job["id"]}.json'))

# ── 认证 ──────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    pwd = data.get('password', '')
    if pwd == PASSWORD:
        session['authenticated'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': '密码错误'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('authenticated', None)
    return jsonify({'ok': True})

@app.before_request
def check_auth():
    maybe_cleanup_expired_local_artifacts()
    public_endpoints = {
        'static', 'index', 'login', 'logout', 'api_check_auth', 'list_bgm',
        # 主题推荐只是公开本地缓存（GET 读取 + POST 触发本地脚本刷新列表顺序），
        # 允许未登录读写，避免登录 cookie/代理抖动导致首屏报错或刷新按钮被静默吞掉
        'topic_recommendations',
        'refresh_topic_recommendations',
        'get_music_style_tags',
    }
    if request.endpoint in public_endpoints:
        return
    if session.get('authenticated') is not True:
        return jsonify({'error': '未登录'}), 401

@app.after_request
def no_cache_for_web_ui(response):
    if request.endpoint == 'index':
        response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response

@app.route('/api/check-auth')
def api_check_auth():
    return jsonify({'authenticated': session.get('authenticated') is True})

# ── 核心接口 ──────────────────────────────────────────────
@app.route('/api/jobs', methods=['POST'])
def create_job():
    """创建一个新任务：主题创作 或 直接文稿"""
    data = request.get_json()
    mode = data.get('mode')  # 'theme' | 'script'

    job_id = str(uuid.uuid4())[:8]

    if mode == 'theme':
        theme = (data.get('theme') or '').strip()
        if not theme:
            return jsonify({'error': '主题不能为空'}), 400
        job = {
            'id': job_id,
            'mode': 'theme',
            'theme': theme,
            'status': 'pending',   # pending → writing → ready → tts → mixing → done
            'script': None,
            'edited_script': None,
            'voice': data.get('voice') or 'zh-CN-YunzeNeural',
            'audio_url': None,
            'final_url': None,
            'error': None,
            'created_at': datetime.now().isoformat(),
            'bgm': data.get('bgm', True),
            'bgm_asset': data.get('bgm_asset') or 'bgm_default.mp3',
            'bgm_volume': data.get('bgm_volume'),
            'voice_runs': [],
        }
    elif mode == 'script':
        script = (data.get('script') or '').strip()
        if not script:
            return jsonify({'error': '文稿不能为空'}), 400
        job = {
            'id': job_id,
            'mode': 'script',
            'theme': None,
            'script': script,
            'edited_script': None,
            'status': 'ready',     # 文稿模式直接可以 TTS
            'audio_url': None,
            'final_url': None,
            'error': None,
            'created_at': datetime.now().isoformat(),
            'bgm': data.get('bgm', True),
            'bgm_asset': data.get('bgm_asset') or 'bgm_default.mp3',
            'bgm_volume': data.get('bgm_volume'),
            'voice_runs': [],
        }
    elif mode == 'music':
        theme = (data.get('theme') or '').strip()
        if not theme:
            return jsonify({'error': '主题/风格描述不能为空'}), 400
        job = {
            'id': job_id,
            'mode': 'music',
            'theme': theme,
            'title': data.get('title') or '',
            'style_tags': '',
            'style_description': (data.get('style_description') or '').strip(),
            'reference_audio_url': (data.get('reference_audio_url') or '').strip() or None,
            'reference_audio_name': (data.get('reference_audio_name') or '').strip() or None,
            'lyrics': None,
            'edited_lyrics': None,
            'status': 'pending',   # pending → lyrics_ready → generating → done
            'audio_url': None,
            'audio_path': None,
            'audio_duration_ms': None,
            'error': None,
            'is_instrumental': data.get('is_instrumental', False),
            'is_starred': False,
            'created_at': datetime.now().isoformat(),
        }
    elif mode == 'music_cover':
        cover_prompt = (data.get('cover_prompt') or '').strip()
        if not cover_prompt:
            return jsonify({'error': '目标风格/音色描述不能为空'}), 400
        # 参考音频允许在创建后上传（job 创建 → 上传 → 生成）。
        # 当有 reference_audio_url 时直接进 pending；没有时进 awaiting_reference，UI 上传后由前端 PATCH 切换。
        reference_audio_url = (data.get('reference_audio_url') or '').strip() or None
        job = {
            'id': job_id,
            'mode': 'music_cover',
            'reference_audio_url': reference_audio_url,
            'reference_audio_name': (data.get('reference_audio_name') or '').strip() or None,
            'cover_prompt': cover_prompt,
            'cover_mode': data.get('cover_mode') or 'one_step',   # 'one_step' | 'two_step'
            'cover_feature_id': None,
            'formatted_lyrics': None,
            'lyrics': None,
            'edited_lyrics': None,
            'status': 'pending' if reference_audio_url else 'awaiting_reference',
            'audio_url': None,
            'audio_path': None,
            'audio_duration_ms': None,
            'error': None,
            'is_starred': False,
            'created_at': datetime.now().isoformat(),
        }
    else:
        return jsonify({'error': 'mode 必须是 theme、script 或 music'}), 400

    save_job(job)
    if mode == 'theme':
        _trigger_writer()
    return jsonify({'job_id': job_id, 'job': job_response(job)})

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify(job_response(job))

@app.route('/api/jobs/<job_id>', methods=['PATCH'])
def update_job(job_id):
    """主 session 更新任务状态，或用户编辑文稿"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    data = request.get_json() or {}

    # 用户可能直接编辑文稿
    if 'edited_script' in data:
        job['edited_script'] = data['edited_script']

    # 主 session 更新状态
    for key in (
        'status', 'script', 'edited_script', 'edited_lyrics',
        'style_description', 'style_tags', 'title',
        'reference_audio_url', 'reference_audio_name',
        'cover_prompt', 'cover_mode', 'cover_feature_id', 'formatted_lyrics',
        'is_starred', 'is_instrumental',
        'voice', 'bgm', 'bgm_asset',
        'audio_url', 'final_url', 'audio_path', 'audio_duration_ms',
        'error',
    ):
        if key in data:
            job[key] = data[key]

    save_job(job)
    return jsonify(job_response(job))

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job_api(job_id):
    """删除任务：从当前列表移除，并清理该任务生成的本地音频。"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    delete_job(job)
    return jsonify({'ok': True})

@app.route('/api/jobs/<job_id>/approve', methods=['POST'])
def approve_job(job_id):
    """用户审批通过文稿；不自动进入 TTS。"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    # 使用编辑后的文稿（如果有）
    script = job.get('edited_script') or job.get('script')
    if not script:
        return jsonify({'error': '文稿为空，无法生成'}), 400

    job['status'] = 'ready'
    job['approved_at'] = datetime.now().isoformat()
    save_job(job)
    return jsonify(job_response(job))

@app.route('/api/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    """重新创作（仅 theme 模式）"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    cleanup_job_outputs(job)
    job['status'] = 'pending'
    for key in (
        'script', 'edited_script', 'audio_url', 'final_url', 'audio_path',
        'final_path', 'approved_at', 'error'
    ):
        job[key] = None
    save_job(job)
    _trigger_writer()
    return jsonify(job_response(job))

@app.route('/api/jobs/<job_id>/archive', methods=['POST'])
def archive_job_api(job_id):
    """归档任务（主 session 在完成后调用）"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    archive_job(job)
    return jsonify({'ok': True})

# ── Music Endpoints ───────────────────────────────────────

def _run_lyrics_generation(job_id, full_prompt):
    """Background worker: run the lyrics sub-process and persist the result."""
    try:
        result = subprocess.run(
            ['python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
             'lyrics', '--prompt', full_prompt],
            text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or '歌词生成失败')

        lyrics_data = json.loads(result.stdout)
        job = load_job(job_id)
        job['lyrics'] = lyrics_data.get('lyrics', '')
        job['edited_lyrics'] = None
        job['title'] = lyrics_data.get('song_title', '') or job.get('title', '')
        job['style_tags'] = lyrics_data.get('style_tags', '')
        job['status'] = 'lyrics_ready'
        job['error'] = None
        save_job(job)
    except Exception as exc:
        job = load_job(job_id)
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)


@app.route('/api/jobs/<job_id>/generate-lyrics', methods=['POST'])
def generate_lyrics_api(job_id):
    """Step 1: Generate lyrics from theme using MiniMax Lyrics API.

    Returns immediately with status='generating_lyrics'; the sub-process runs
    in a background thread. The frontend should poll /api/jobs/<id> to follow
    progress and pick up the final status.
    """
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    theme = job.get('theme', '')
    if not theme:
        return jsonify({'error': '主题为空'}), 400

    style_desc = (job.get('style_description') or '').strip()
    full_prompt = theme
    if style_desc:
        full_prompt = f"{theme}\n\n风格描述：{style_desc}"

    job['status'] = 'generating_lyrics'
    job['error'] = None
    save_job(job)

    threading.Thread(
        target=_run_lyrics_generation,
        args=(job_id, full_prompt),
        daemon=True,
    ).start()

    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>/generate-music', methods=['POST'])
def generate_music_api(job_id):
    """Step 2: Generate music from lyrics using MiniMax Music API."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    lyrics_text = (job.get('edited_lyrics') or job.get('lyrics') or '').strip()
    is_instrumental = job.get('is_instrumental', False)
    if not lyrics_text and not is_instrumental:
        return jsonify({'error': '歌词为空，请先生成或编辑歌词'}), 400

    prompt = job.get('theme', '')
    if job.get('style_tags'):
        prompt = prompt + ', ' + job['style_tags']
    if job.get('style_description'):
        prompt = prompt + '\n\n' + job['style_description']

    run_dir = RUNS_DIR / job['id']
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / 'music.mp3'

    lyrics_path = None

    # Build CLI args
    cmd = [
        'python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
        'music',
        '--prompt', prompt,
        '-o', str(output_path),
        '--model', 'music-2.6',
    ]
    if is_instrumental:
        cmd.append('--instrumental')
    else:
        lyrics_path = run_dir / 'lyrics.txt'
        lyrics_path.write_text(lyrics_text, encoding='utf-8')
        cmd.extend(['--lyrics-file', str(lyrics_path)])

    job['status'] = 'generating'
    job['error'] = None
    save_job(job)

    def _run_music_generation():
        try:
            result = subprocess.run(cmd, text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=600)
            if result.returncode != 0:
                raise RuntimeError(result.stderr or result.stdout or '音乐生成失败')

            # Try to capture duration / size from the script's last JSON line
            try:
                music_meta = json.loads(result.stdout.strip().splitlines()[-1])
                if isinstance(music_meta, dict) and music_meta.get('duration_ms'):
                    job['audio_duration_ms'] = int(music_meta['duration_ms'])
            except (ValueError, IndexError):
                pass

            # ── 音质质量保证 (2026-06-09) ───────────────────────────────
            # User requirement: every generated music must be at least
            #   320 kbps MP3 (CBR), 44.1 kHz, stereo.
            # MiniMax / MiniMax music-2.6 returns 256 kbps by default,
            # so we re-encode with libmp3lame CBR to meet the standard.
            try:
                tmp_320 = run_dir / 'music.320k.mp3'
                _ffmpeg_qa = subprocess.run(
                    [
                        'ffmpeg', '-y', '-loglevel', 'error',
                        '-i', str(output_path),
                        '-c:a', 'libmp3lame',
                        '-b:a', '320k',
                        '-ar', '44100',
                        '-ac', '2',
                        '-reservoir', '0',  # force CBR (no VBR reservoir)
                        '-write_xing', '1',
                        str(tmp_320),
                    ],
                    text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=120,
                )
                if _ffmpeg_qa.returncode == 0 and tmp_320.exists() and tmp_320.stat().st_size > 0:
                    # Replace the original with the 320k version
                    shutil.move(str(tmp_320), str(output_path))
                    job['audio_quality'] = {
                        'bitrate_kbps': 320,
                        'sample_rate_hz': 44100,
                        'channels': 2,
                        'codec': 'mp3',
                        'mode': 'CBR',
                    }
                else:
                    job['audio_quality'] = {
                        'bitrate_kbps': 256,
                        'sample_rate_hz': 44100,
                        'channels': 2,
                        'codec': 'mp3',
                        'mode': 'default',
                        'warning': '320kbps re-encode failed; using provider default',
                    }
            except Exception as _qa_exc:
                job['audio_quality'] = {
                    'bitrate_kbps': 256,
                    'sample_rate_hz': 44100,
                    'channels': 2,
                    'codec': 'mp3',
                    'mode': 'default',
                    'warning': f'320kbps re-encode skipped: {_qa_exc}',
                }

            # Upload to R2 with readable names.
            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            upload_result = subprocess.run(
                ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
                 '--file', str(output_path), '--theme', 'music',
                 '--name', oss_object_name(job, 'song', 'mp3', ts=ts)],
                text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=60,
            )
            if upload_result.returncode == 0:
                audio_url = upload_result.stdout.strip().splitlines()[-1].strip()
            else:
                audio_url = None

            lyrics_url = None
            if lyrics_text:
                effective_lyrics_path = lyrics_path if lyrics_path is not None else (run_dir / 'lyrics.txt')
                if not effective_lyrics_path.exists():
                    effective_lyrics_path.write_text(lyrics_text, encoding='utf-8')
                lyrics_upload = subprocess.run(
                    ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
                     '--file', str(effective_lyrics_path), '--theme', 'music',
                     '--name', oss_object_name(job, 'lyrics', 'txt', ts=ts)],
                    text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=60,
                )
                if lyrics_upload.returncode == 0:
                    lyrics_url = lyrics_upload.stdout.strip().splitlines()[-1].strip()

            job['audio_path'] = str(output_path)
            job['audio_url'] = audio_url
            if lyrics_url:
                job['lyrics_url'] = lyrics_url
            job['status'] = 'done'
            job['error'] = None
            # Append this new generation to music_runs history
            runs = list(job.get('music_runs') or [])
            for r in runs:
                r['is_current'] = False
            runs.append({
                'run_id': f"r{len(runs)+1}-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
                'audio_url': audio_url,
                'lyrics_url': lyrics_url,
                'lyrics': lyrics_text,
                'audio_duration_ms': job.get('audio_duration_ms'),
                'audio_quality': job.get('audio_quality'),
                'created_at': datetime.now().isoformat(),
                'is_current': True,
                'label': '当前版本',
            })
            job['music_runs'] = runs
            save_job(job)
        except Exception as exc:
            job['status'] = 'error'
            job['error'] = str(exc)
            save_job(job)

    threading.Thread(target=_run_music_generation, daemon=True).start()

    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>/retry-lyrics', methods=['POST'])
def retry_lyrics_api(job_id):
    """Regenerate lyrics for a music job (async, returns immediately)."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    theme = job.get('theme', '')
    if not theme:
        return jsonify({'error': '主题为空'}), 400

    job['lyrics'] = None
    job['edited_lyrics'] = None
    job['style_tags'] = ''
    job['audio_url'] = None
    job['audio_path'] = None
    job['status'] = 'generating_lyrics'
    job['error'] = None
    save_job(job)

    style_desc = (job.get('style_description') or '').strip()
    full_prompt = theme
    if style_desc:
        full_prompt = f"{theme}\n\n风格描述：{style_desc}"

    threading.Thread(
        target=_run_lyrics_generation,
        args=(job_id, full_prompt),
        daemon=True,
    ).start()

    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>/retry-music', methods=['POST'])
def retry_music_api(job_id):
    """Regenerate music from existing lyrics.
    Preserves the previous version into job['music_runs'] history
    and replaces job['audio_url']/['lyrics_url'] with the new result.
    """
    original = load_job(job_id)
    if not original:
        return jsonify({'error': '任务不存在'}), 404
    if original.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    lyrics_text = (original.get('edited_lyrics') or original.get('lyrics') or '').strip()
    is_instrumental = original.get('is_instrumental', False)
    if not lyrics_text and not is_instrumental:
        return jsonify({'error': '歌词为空，请先生成或编辑歌词'}), 400

    # Snapshot the CURRENT run into music_runs history (only if it has audio)
    runs = list(original.get('music_runs') or [])
    if original.get('audio_url') or original.get('audio_path'):
        # Mark the previous current as not-current and snapshot it
        for r in runs:
            r['is_current'] = False
        runs.append({
            'run_id': f"r{len(runs)+1}-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            'audio_url': original.get('audio_url'),
            'lyrics_url': original.get('lyrics_url'),
            'lyrics': original.get('lyrics') or original.get('edited_lyrics'),
            'audio_duration_ms': original.get('audio_duration_ms'),
            'audio_quality': original.get('audio_quality'),
            'created_at': original.get('updated_at') or original.get('created_at'),
            'is_current': False,
            'label': f"第 {len(runs)+1} 版",
        })

    # Reset current run to empty (will be filled by generate_music_api)
    original['audio_url'] = None
    original['audio_path'] = None
    original['lyrics_url'] = None
    original['status'] = 'lyrics_ready'
    original['error'] = None
    original['music_runs'] = runs
    save_job(original)

    return generate_music_api(job_id)


@app.route('/api/jobs/<job_id>/switch-music-run', methods=['POST'])
def switch_music_run_api(job_id):
    """Switch the active 'current' music_runs entry for a music job.
    Body: { "run_id": "r2-..." }
    Promotes the chosen historical run to the top-level audio_url/lyrics
    and marks the current one as history (does NOT delete anything).
    """
    original = load_job(job_id)
    if not original:
        return jsonify({'error': '任务不存在'}), 404
    if original.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    body = request.get_json(silent=True) or {}
    target_run_id = body.get('run_id')
    if not target_run_id:
        return jsonify({'error': '缺少 run_id'}), 400

    runs = list(original.get('music_runs') or [])
    if not runs:
        return jsonify({'error': '没有可切换的历史版本'}), 400

    target = None
    for r in runs:
        if r.get('run_id') == target_run_id:
            target = r
            break
    if not target:
        return jsonify({'error': '找不到指定版本'}), 404

    # Snapshot the current run (if it has audio) into history
    if original.get('audio_url') or original.get('audio_path'):
        for r in runs:
            r['is_current'] = False
        runs.append({
            'run_id': f"r{len(runs)+1}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-switch",
            'audio_url': original.get('audio_url'),
            'lyrics_url': original.get('lyrics_url'),
            'lyrics': original.get('lyrics') or original.get('edited_lyrics'),
            'audio_duration_ms': original.get('audio_duration_ms'),
            'audio_quality': original.get('audio_quality'),
            'created_at': original.get('updated_at') or original.get('created_at'),
            'is_current': False,
            'label': f"第 {len(runs)+1} 版",
        })
        # Remove the original target from runs so we don't duplicate
        runs = [r for r in runs if r.get('run_id') != target_run_id]

    # Promote target to current
    original['audio_url'] = target.get('audio_url')
    original['lyrics_url'] = target.get('lyrics_url')
    original['lyrics'] = target.get('lyrics')
    original['edited_lyrics'] = None
    original['audio_duration_ms'] = target.get('audio_duration_ms')
    original['status'] = 'done'
    original['error'] = None
    # Mark the (now top-level) target as current in the list
    for r in runs:
        r['is_current'] = False
    runs.append({
        'run_id': target.get('run_id') + '-current',
        'audio_url': target.get('audio_url'),
        'lyrics_url': target.get('lyrics_url'),
        'lyrics': target.get('lyrics'),
        'audio_duration_ms': target.get('audio_duration_ms'),
        'created_at': target.get('created_at') or datetime.now().isoformat(),
        'is_current': True,
        'label': '当前版本',
    })
    original['music_runs'] = runs
    save_job(original)
    return jsonify(job_response(original))


# ── Music Cover Endpoints (翻唱) ──────────────────────────

@app.route('/api/jobs/<job_id>/cover-preprocess', methods=['POST'])
def cover_preprocess_api(job_id):
    """Two-step cover step 1: extract feature id + structured lyrics from
    the uploaded reference audio. Persists cover_feature_id + formatted_lyrics
    on the job, then status flips to 'preprocess_ready' so the user can edit
    lyrics before generating."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music_cover':
        return jsonify({'error': '非翻唱任务'}), 400
    if not job.get('reference_audio_url'):
        return jsonify({'error': '缺少参考音频'}), 400

    if job.get('status') == 'awaiting_reference':
        job['status'] = 'pending'
    job['status'] = 'preprocessing'
    job['error'] = None
    save_job(job)

    def _run():
        try:
            result = subprocess.run(
                ['python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
                 'preprocess', '--cover-url', job['reference_audio_url']],
                text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or '预处理失败').strip()[-1000:])
            data = json.loads(result.stdout)
            j = load_job(job_id)
            j['cover_feature_id'] = data.get('cover_feature_id')
            j['formatted_lyrics'] = data.get('formatted_lyrics') or ''
            j['lyrics'] = j['formatted_lyrics']
            j['edited_lyrics'] = None
            j['status'] = 'preprocess_ready'
            j['error'] = None
            save_job(j)
        except Exception as exc:
            j = load_job(job_id)
            j['status'] = 'error'
            j['error'] = str(exc)
            save_job(j)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>/cover-generate', methods=['POST'])
def cover_generate_api(job_id):
    """Generate the cover track. Two paths share this endpoint:
      - one_step:  --cover-url + --cover-prompt (lyrics preserved from reference)
      - two_step:  --cover-feature-id + --lyrics (user may rewrite)
    Both run async; the frontend polls /api/jobs/<id> for status."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music_cover':
        return jsonify({'error': '非翻唱任务'}), 400
    if not job.get('cover_prompt'):
        return jsonify({'error': '目标风格/音色描述为空'}), 400
    if not job.get('reference_audio_url'):
        return jsonify({'error': '请先上传参考曲'}), 400

    cover_mode = job.get('cover_mode') or 'one_step'
    feature_id = job.get('cover_feature_id')
    if cover_mode == 'two_step' and not feature_id:
        return jsonify({'error': '两步模式需先调用 /cover-preprocess'}), 400

    lyrics_text = (job.get('edited_lyrics') or job.get('lyrics') or '').strip()

    # Two-Step mode requires usable lyrics. Reject invalid ones (empty / [Silence] / too short)
    # so we surface a clear error before the MiniMax API returns "lyrics is too short".
    if cover_mode == 'two_step':
        invalid_markers = ('', '[silence]', '[silence]\n')
        normalized = lyrics_text.strip().lower()
        if normalized in invalid_markers or len(lyrics_text) < 20:
            return jsonify({
                'error': '歌词无效（空 / [Silence] / 太短）。请在「改写后的歌词」编辑器里手动粘贴原曲歌词后再生成。',
                'code': 'lyrics_invalid',
                'job_status': job.get('status'),
            }), 400

    run_dir = RUNS_DIR / job['id']
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / 'cover.mp3'
    lyrics_path = run_dir / 'cover_lyrics.txt'

    model = 'music-cover-free' if os.environ.get('VOICE_STUDIO_COVER_USE_FREE') else 'music-cover'
    cmd = [
        'python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
        'music',
        '--prompt', job['cover_prompt'],
        '-o', str(output_path),
        '--model', model,
    ]
    if cover_mode == 'two_step':
        cmd.extend(['--cover-feature-id', feature_id])
        if lyrics_text:
            lyrics_path.write_text(lyrics_text, encoding='utf-8')
            cmd.extend(['--lyrics-file', str(lyrics_path)])
    else:
        cmd.extend(['--cover-url', job['reference_audio_url']])

    job['status'] = 'generating'
    job['error'] = None
    save_job(job)

    def _run():
        try:
            # 如果是 two_step 且 cover_feature_id 过期（>24h），MiniMax 会返回
            # "invalid cover_feature_id"。在两-step 流程中遇到这个错误时，自动重跑 preprocess
            # 并用新 ID + 用户已有的 edited_lyrics 重试一次。
            nonlocal_feature_retry_used = False

            def _try_cover_generate(local_cmd):
                local_result = subprocess.run(
                    local_cmd, text=True, capture_output=True,
                    cwd=str(SKILL_DIR), timeout=600,
                )
                if local_result.returncode != 0:
                    err = (local_result.stderr or local_result.stdout or '翻唱生成失败').strip()[-1200:]
                    return local_result, err
                return local_result, None

            result, err = _try_cover_generate(cmd)

            # 自动恢复：two_step + cover_feature_id 失效 + 未重试过 → 重跑 preprocess
            if (err and cover_mode == 'two_step' and feature_id
                    and 'invalid cover_feature_id' in err
                    and not nonlocal_feature_retry_used):
                nonlocal_feature_retry_used = True
                j = load_job(job_id)
                # 状态切换到 preprocessing 并保存，给用户一个反馈
                j['status'] = 'preprocessing'
                j['error'] = 'cover_feature_id 已过期（>24h），正在自动重新提取…'
                save_job(j)
                try:
                    prep = subprocess.run(
                        ['python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
                         'preprocess', '--cover-url', job['reference_audio_url']],
                        text=True, capture_output=True,
                        cwd=str(SKILL_DIR), timeout=120,
                    )
                    if prep.returncode != 0:
                        raise RuntimeError('重新提取失败：' + (prep.stderr or prep.stdout or '').strip()[-400:])
                    prep_data = json.loads(prep.stdout)
                    new_fid = prep_data.get('cover_feature_id')
                    if not new_fid:
                        raise RuntimeError('重新提取未返回 cover_feature_id')
                    # 用新 ID 重置 job 字段；保留用户手动填的 edited_lyrics
                    j['cover_feature_id'] = new_fid
                    j['formatted_lyrics'] = prep_data.get('formatted_lyrics') or ''
                    if not (j.get('edited_lyrics') or '').strip():
                        # 用户没手动填过 → 同步用最新抽取的 lyrics
                        j['lyrics'] = j['formatted_lyrics']
                    j['error'] = None
                    save_job(j)
                    # 重置 feature_id 局部变量 + 重新拼装 cmd
                    new_cmd = [
                        'python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
                        'music',
                        '--prompt', job['cover_prompt'],
                        '-o', str(output_path),
                        '--model', model,
                        '--cover-feature-id', new_fid,
                    ]
                    if lyrics_text:
                        lyrics_path.write_text(lyrics_text, encoding='utf-8')
                        new_cmd.extend(['--lyrics-file', str(lyrics_path)])
                    j['status'] = 'generating'
                    save_job(j)
                    result, err = _try_cover_generate(new_cmd)
                except Exception as recover_exc:
                    raise RuntimeError(f'cover_feature_id 过期且自动重提取失败：{recover_exc}')

            if err:
                raise RuntimeError(err)

            try:
                music_meta = json.loads(result.stdout.strip().splitlines()[-1])
                if isinstance(music_meta, dict) and music_meta.get('duration_ms'):
                    job['audio_duration_ms'] = int(music_meta['duration_ms'])
            except (ValueError, IndexError):
                pass

            ts = datetime.now().strftime('%Y%m%d-%H%M%S')
            upload = subprocess.run(
                ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
                 '--file', str(output_path), '--theme', 'cover',
                 '--name', oss_object_name(job, 'cover', 'mp3', ts=ts)],
                text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=60,
            )
            if upload.returncode == 0:
                audio_url = upload.stdout.strip().splitlines()[-1].strip()
            else:
                audio_url = None

            lyrics_url = None
            if lyrics_text and cover_mode == 'two_step':
                if not lyrics_path.exists():
                    lyrics_path.write_text(lyrics_text, encoding='utf-8')
                lyric_up = subprocess.run(
                    ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
                     '--file', str(lyrics_path), '--theme', 'cover',
                     '--name', oss_object_name(job, 'lyrics', 'txt', ts=ts)],
                    text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=60,
                )
                if lyric_up.returncode == 0:
                    lyrics_url = lyric_up.stdout.strip().splitlines()[-1].strip()

            j = load_job(job_id)
            j['audio_path'] = str(output_path)
            j['audio_url'] = audio_url
            if lyrics_url:
                j['lyrics_url'] = lyrics_url
            j['status'] = 'done'
            j['error'] = None
            save_job(j)
        except Exception as exc:
            j = load_job(job_id)
            j['status'] = 'error'
            j['error'] = str(exc)
            save_job(j)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(job_response(job))


@app.route('/api/jobs/cover', methods=['GET'])
def list_cover_jobs():
    return list_jobs_by_mode('cover')


# ── Music: 参考音乐上传 / 收藏 ────────────────────────────────
ALLOWED_REFERENCE_AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}
MAX_REFERENCE_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB

@app.route('/api/jobs/<job_id>/reference-audio', methods=['POST'])
def upload_reference_audio_api(job_id):
    """Upload a reference audio file for a music / music_cover job. Stored on R2;
    URL is persisted to job.json. For music_cover it is the actual reference input;
    for music-2.6 jobs the URL is kept as a future-use asset."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') not in ('music', 'music_cover'):
        return jsonify({'error': '非音乐/翻唱任务'}), 400

    if 'file' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400
    f = request.files['file']
    if not f or not f.filename:
        return jsonify({'error': '文件为空'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_REFERENCE_AUDIO_EXTS:
        return jsonify({'error': f'不支持的格式 {ext}（仅 {", ".join(sorted(ALLOWED_REFERENCE_AUDIO_EXTS))}）'}), 400

    # Save to a temp file under runs/<job_id>/reference.<ext>
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = run_dir / f'reference{ext}'
    f.save(tmp_path)
    if tmp_path.stat().st_size > MAX_REFERENCE_AUDIO_BYTES:
        tmp_path.unlink(missing_ok=True)
        return jsonify({'error': f'参考音频超过 {MAX_REFERENCE_AUDIO_BYTES // (1024*1024)} MB 上限'}), 400

    # Upload to R2
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    try:
        result = subprocess.run(
            ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
             '--file', str(tmp_path), '--theme', job.get('mode', 'music'),
             '--name', oss_object_name(job, 'reference', ext.lstrip('.'), ts=ts)],
            text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=60,
        )
    except subprocess.TimeoutExpired:
        return jsonify({'error': '上传超时'}), 500
    if result.returncode != 0:
        return jsonify({'error': (result.stderr or result.stdout or '上传失败').strip()}), 500

    audio_url = result.stdout.strip().splitlines()[-1].strip()
    job['reference_audio_url'] = audio_url
    job['reference_audio_name'] = f.filename
    # cover 任务：上传参考音频后从 awaiting_reference 回到 pending
    if job.get('mode') == 'music_cover' and job.get('status') == 'awaiting_reference':
        job['status'] = 'pending'
    save_job(job)
    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>/reference-audio', methods=['DELETE'])
def delete_reference_audio_api(job_id):
    """Clear the reference audio from job.json (does not touch R2 object)."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') not in ('music', 'music_cover'):
        return jsonify({'error': '非音乐/翻唱任务'}), 400
    job['reference_audio_url'] = None
    job['reference_audio_name'] = None
    save_job(job)
    return jsonify(job_response(job))


@app.route('/api/jobs/<job_id>/star', methods=['POST'])
def star_job_api(job_id):
    """Toggle is_starred on a job (any mode supported, but UI only shows star
    on music / cover tracks for now)."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    data = request.get_json(silent=True) or {}
    new_value = data.get('is_starred')
    if new_value is None:
        new_value = not job.get('is_starred', False)
    job['is_starred'] = bool(new_value)
    save_job(job)
    return jsonify(job_response(job))


@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    """列出当前活跃任务（不包括已归档）。?mode=voice|music|cover"""
    mode_filter = request.args.get('mode')
    jobs = []
    modes_to_scan = [mode_filter] if mode_filter in MODE_CONFIG else list(MODE_CONFIG.keys())
    for m in modes_to_scan:
        d = MODE_CONFIG[m]['job_dir']
        for fname in os.listdir(d):
            if fname.endswith('.json'):
                try:
                    with open(os.path.join(d, fname), encoding='utf-8') as f:
                        jobs.append(json.load(f))
                except json.JSONDecodeError:
                    continue
    jobs.sort(key=lambda x: x['created_at'], reverse=True)
    return jsonify([job_response(job) for job in jobs])


@app.route('/api/jobs/voice', methods=['GET'])
def list_voice_jobs():
    limit, offset = _parse_pagination()
    if limit is None:
        return list_jobs_by_mode('voice')
    return list_jobs_by_mode('voice', limit=limit, offset=offset)


@app.route('/api/jobs/music', methods=['GET'])
def list_music_jobs():
    limit, offset = _parse_pagination()
    if limit is None:
        return list_jobs_by_mode('music')
    return list_jobs_by_mode('music', limit=limit, offset=offset)


def _parse_pagination():
    """Read ?limit=&offset= from request args.

    Returns (limit, offset). limit is None when ?limit= is absent
    (caller decides whether to fall back to "return everything" or
    apply a default). Negative values are clamped to safe defaults.
    """
    limit = request.args.get('limit', type=int)
    offset = request.args.get('offset', default=0, type=int)
    if limit is not None and limit < 0:
        limit = None
    if offset < 0:
        offset = 0
    return limit, offset


def list_jobs_by_mode(mode, limit=None, offset=0):
    jobs = []
    d = job_dir(mode)
    for fname in os.listdir(d):
        if fname.endswith('.json'):
            try:
                with open(os.path.join(d, fname), encoding='utf-8') as f:
                    jobs.append(json.load(f))
            except json.JSONDecodeError:
                continue
    jobs.sort(key=lambda x: x['created_at'], reverse=True)

    if limit is None:
        return jsonify([job_response(job) for job in jobs])

    total = len(jobs)
    page = jobs[offset:offset + limit]
    return jsonify({
        'jobs': [job_response(job) for job in page],
        'total': total,
        'limit': limit,
        'offset': offset,
    })

@app.route('/api/topic-recommendations', methods=['GET'])
def topic_recommendations():
    try:
        return jsonify(load_topic_recommendations())
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({'error': f'推荐主题读取失败：{exc}'}), 500

@app.route('/api/topic-recommendations/refresh', methods=['POST'])
def refresh_topic_recommendations():
    try:
        script = os.path.join(os.path.dirname(__file__), 'scripts', 'refresh_topics.py')
        result = subprocess.run(
            ['python3', script],
            capture_output=True, text=True, timeout=90
        )
        if result.returncode != 0:
            tail = (result.stderr or '').strip().splitlines()[-3:]
            return jsonify({
                'error': '刷新脚本退出非零',
                'stderr_tail': tail,
                'mode_note': (result.stdout or '').strip().splitlines()[-1] if result.stdout else '',
            }), 500
        # 脚本最后一行 stdout 是 JSON：{'mode': 'fetch+llm' | 'shuffle', ...}
        last_line = (result.stdout or '').strip().splitlines()[-1] if result.stdout else ''
        topics = load_topic_recommendations()
        # 顺手把脚本的 note 拼进响应头让前端可以拿到，但不返回原始 list 双重负载
        resp = jsonify(topics)
        if last_line.startswith('{') and 'mode' in last_line:
            try:
                meta = json.loads(last_line)
                resp.headers['X-Refresh-Mode'] = meta.get('mode', '')
                resp.headers['X-Refresh-Note'] = (meta.get('note') or '')[:80]
            except Exception:
                pass
        return resp
    except subprocess.TimeoutExpired:
        return jsonify({'error': '刷新超时（60s）'}), 500
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

MUSIC_STYLE_TAGS_FILE = os.path.join(os.path.dirname(__file__), 'music_style_tags.json')

def load_music_style_tags():
    with open(MUSIC_STYLE_TAGS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

@app.route('/api/music-style-tags', methods=['GET'])
def get_music_style_tags():
    import random
    try:
        all_tags = load_music_style_tags()
        count = min(18, len(all_tags))
        return jsonify(random.sample(all_tags, count))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({'error': f'风格标签读取失败：{exc}'}), 500

def safe_name(value, fallback='voice', max_len=80):
    """Return a readable object-key fragment while preserving Chinese titles."""
    value = (value or '').strip()
    if not value:
        value = fallback
    chars = []
    for char in value:
        if char.isalnum() or char in ('.', '_', '-'):
            chars.append(char)
        elif char.isspace() or char in '，。、《》【】（）()[]{}：:；;、/\\|!?！？“”"\'`~@#$%^&*+=,':
            chars.append('-')
    slug = re.sub(r'-{2,}', '-', ''.join(chars)).strip('-._')
    return (slug[:max_len].strip('-._') or fallback)

def job_name_fragment(job, fallback='untitled'):
    """Pick the most human-readable job label for uploaded filenames."""
    candidates = [
        job.get('title'),
        job.get('theme'),
        job.get('edited_script'),
        job.get('script'),
        job.get('edited_lyrics'),
        job.get('lyrics'),
    ]
    for value in candidates:
        text = (value or '').strip()
        if text:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
            return safe_name(first_line, fallback=fallback, max_len=48)
    return fallback

def oss_object_name(job, artifact, ext, run_id=None, ts=None):
    """Readable OSS filename: type-title-time-shortid-artifact.ext."""
    mode = 'music' if job.get('mode') == 'music' else 'voice'
    title = job_name_fragment(job, fallback=mode)
    timestamp = ts or datetime.now().strftime('%Y%m%d-%H%M%S')
    short_id = safe_name(job.get('id', '')[:8] or 'job', fallback='job', max_len=16)
    if run_id:
        run_suffix = safe_name(run_id.split('-')[-1], fallback='run', max_len=12)
        short_id = f'{short_id}-r{run_suffix}'
    artifact_slug = safe_name(artifact, fallback='file', max_len=24)
    ext = ext.lstrip('.')
    return f'{mode}-{title}-{timestamp}-{short_id}-{artifact_slug}.{ext}'

def run_cmd(cmd, cwd=None):
    result = subprocess.run(
        cmd,
        cwd=cwd or str(SKILL_DIR),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or '命令执行失败').strip()[-1200:])
    return result.stdout.strip()

def split_text_chunks(text, limit=900):
    """Split long narration into Azure-safe paragraph groups."""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    chunks = []
    current = ''
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = paragraph if not current else current + '\n\n' + paragraph
    if current:
        chunks.append(current)
    return chunks

def synthesize_azure_chunked(script, run_dir, voice_path, voice='zh-CN-YunzeNeural'):
    """Generate long Azure narration in chunks, then concatenate as one MP3."""
    chunk_dir = run_dir / 'azure_chunks'
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_text_chunks(script)
    chunk_paths = []


    for index, chunk in enumerate(chunks, 1):
        text_path = chunk_dir / f'chunk_{index:02d}.txt'
        audio_path = chunk_dir / f'chunk_{index:02d}.mp3'
        text_path.write_text(chunk, encoding='utf-8')


        cmd = [
            'python3', str(SKILL_DIR / 'scripts' / 'azure_tts.py'),
            '--file', str(text_path),
            '--voice', voice,
            '--style', 'calm',
            '--rate=-10%',
            '--pause-ms', '800',
            '-o', str(audio_path),
        ]
        last_error = ''
        for attempt in range(1, 4):
            result = subprocess.run(cmd, text=True, capture_output=True, cwd=str(SKILL_DIR))
            if result.returncode == 0 and audio_path.exists() and audio_path.stat().st_size > 10000:
                chunk_paths.append(audio_path)
                break
            last_error = (result.stderr or result.stdout or 'Azure chunk failed').strip()[-800:]
            time.sleep(2 * attempt)
        else:
            raise RuntimeError(f'Azure chunk {index}/{len(chunks)} failed: {last_error}')

    concat_list = chunk_dir / 'concat.txt'
    concat_list.write_text(''.join(f"file '{path}'\n" for path in chunk_paths), encoding='utf-8')
    run_cmd([
        'ffmpeg', '-y',
        '-f', 'concat',
        '-safe', '0',
        '-i', str(concat_list),
        '-c', 'copy',
        str(voice_path),
    ])

def _synthesize_run(job, run_id, voice, do_mix, bgm_asset, provider='azure', bgm_volume=None, speed=None):
    """Generate one voice run: TTS + optional mix + upload to OSS. Returns run dict."""
    script = (job.get('edited_script') or job.get('script') or '').strip()
    if not script:
        raise ValueError('文稿为空')

    run_dir = RUNS_DIR / job['id'] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    script_path = run_dir / 'script.txt'
    voice_path = run_dir / 'voice.mp3'
    mixed_path = run_dir / 'mixed.mp3'
    script_path.write_text(script, encoding='utf-8')

    speed_val = speed if speed is not None else 0.9

    if provider == 'minimax':
        run_cmd([
            'python3', str(SKILL_DIR / 'scripts' / 'minimax_tts.py'),
            '--text', str(script_path), '--out', str(voice_path),
            '--voice', voice, '--speed', str(speed_val),
        ])
    else:
        synthesize_azure_chunked(script, run_dir, voice_path, voice)

    bgm_path = BGM_DIR / bgm_asset
    if not bgm_path.exists():
        bgm_path = SKILL_DIR / 'assets' / 'bgm_default.mp3'

    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    theme_slug = job_name_fragment(job, fallback='direct-script')

    # Upload script
    script_url = run_cmd([
        'python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
        '--file', str(script_path), '--theme', theme_slug,
        '--name', oss_object_name(job, 'script', 'txt', run_id=run_id, ts=ts),
    ]).splitlines()[-1].strip()

    # Upload voice
    voice_url = run_cmd([
        'python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
        '--file', str(voice_path), '--theme', theme_slug,
        '--name', oss_object_name(job, 'voice-only', 'mp3', run_id=run_id, ts=ts),
    ]).splitlines()[-1].strip()

    # Mix + upload if needed
    if do_mix and bgm_path.exists():
        run_cmd([
            'python3', str(SKILL_DIR / 'scripts' / 'mix_with_bgm.py'),
            '--voice', str(voice_path), '--bgm', str(bgm_path),
            '--out', str(mixed_path), '--bgm-volume', str(bgm_volume if bgm_volume is not None else 0.06),
        ])
        final_source = mixed_path
        final_artifact = 'mixed-final'
    else:
        final_source = voice_path
        final_artifact = 'voice-final'

    final_url = run_cmd([
        'python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
        '--file', str(final_source), '--theme', theme_slug,
        '--name', oss_object_name(job, final_artifact, 'mp3', run_id=run_id, ts=ts),
    ]).splitlines()[-1].strip()

    # The combined voice file and final output are now on R2, so the Azure
    # chunk directory is no longer needed for recovery.
    chunk_dir = run_dir / 'azure_chunks'
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir, ignore_errors=True)

    return {
        'run_id': run_id,
        'provider': provider,
        'voice': voice,
        'bgm': do_mix,
        'bgm_asset': bgm_asset,
        'bgm_volume': bgm_volume,
        'status': 'done',
        'script_url': script_url,
        'voice_url': voice_url,
        'final_url': final_url,
        'created_at': datetime.now().isoformat(),
    }


@app.route('/api/jobs/<job_id>/process-tts', methods=['POST'])
def process_tts(job_id):
    """
    Create a new voice run with the job's current voice setting.
    Can be called multiple times — each call creates a new run in voice_runs.
    """
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    script = (job.get('edited_script') or job.get('script') or '').strip()
    if not script:
        return jsonify({'error': '文稿为空，无法生成'}), 400

    if job.get('voice_runs') is None:
        job['voice_runs'] = []

    run_id = f'run-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{len(job["voice_runs"])+1}'
    voice = job.get('voice', 'zh-CN-YunzeNeural')
    do_mix = job.get('bgm', True)
    bgm_asset = job.get('bgm_asset', 'bgm_default.mp3')

    try:
        # Guard: if job already has final_url but status is stuck at tts/mixing,
        # it means a prior run completed but didn't update status (e.g. crash/restart mid-save).
        # Mark it done and skip re-synthesis.
        if job.get('final_url') and job.get('status') in ('tts', 'mixing'):
            job['status'] = 'done'
            save_job(job)
            return jsonify(job_response(job))

        job['status'] = 'tts'
        job['error'] = None
        save_job(job)

        run_info = _synthesize_run(job, run_id, voice, do_mix, bgm_asset, provider='azure')
        job['voice_runs'].append(run_info)
        # Mirror last run to top-level fields for backward compatibility
        job['final_url'] = run_info['final_url']
        job['voice_url'] = run_info['voice_url']
        job['status'] = 'done'
        save_job(job)
        return jsonify(job_response(job))
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job_response(job)), 500


@app.route('/api/jobs/<job_id>/tts-voice-run', methods=['POST'])
def tts_voice_run(job_id):
    """
    Create a new voice run with a SPECIFIC voice (for A/B comparison).
    Body: { "voice": "zh-CN-YunxiNeural", "bgm": true, "bgm_asset": "..." }
    """
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    script = (job.get('edited_script') or job.get('script') or '').strip()
    if not script:
        return jsonify({'error': '文稿为空，无法生成'}), 400

    if job.get('voice_runs') is None:
        job['voice_runs'] = []

    data = request.get_json() or {}
    run_id = f'run-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{len(job["voice_runs"])+1}'
    voice = data.get('voice', job.get('voice', 'zh-CN-YunzeNeural'))
    do_mix = data.get('bgm', job.get('bgm', True))
    bgm_asset = data.get('bgm_asset', job.get('bgm_asset', 'bgm_default.mp3'))
    bgm_volume = data.get('bgm_volume', 0.06)

    try:
        # Guard: if job already has final_url but status is stuck, skip re-synthesis
        if job.get('final_url') and job.get('status') in ('tts', 'mixing'):
            job['status'] = 'done'
            save_job(job)
            return jsonify(job_response(job))

        job['status'] = 'tts'
        job['error'] = None
        save_job(job)

        run_info = _synthesize_run(job, run_id, voice, do_mix, bgm_asset, provider=data.get('provider', 'azure'), bgm_volume=bgm_volume, speed=data.get('speed', 0.9))
        job['voice_runs'].append(run_info)
        job['final_url'] = run_info['final_url']
        job['voice_url'] = run_info['voice_url']
        job['status'] = 'done'
        save_job(job)
        return jsonify(job_response(job))
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job_response(job)), 500

# ── BGM 文件服务 ────────────────────────────────────────
ALLOWED_EXT = {'.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac'}

@app.route('/api/bgm', methods=['GET'])
def list_bgm():
    """返回 BGM 目录下所有可用音频文件"""
    files = []
    for f in BGM_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_EXT:
            files.append({'name': f.name, 'size': f.stat().st_size})
    # 也包含内置 BGM
    default = SKILL_DIR / 'assets' / 'bgm_default.mp3'
    if default.exists():
        files.insert(0, {'name': 'bgm_default.mp3', 'size': default.stat().st_size, 'builtin': True})
    return jsonify(files)

@app.route('/api/bgm/upload', methods=['POST'])
def upload_bgm():
    """上传 BGM 音频文件（需认证）"""
    if 'file' not in request.files:
        return jsonify({'error': '没有文件字段'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify({'error': f'不支持的格式 {ext}，支持: {", ".join(sorted(ALLOWED_EXT))}'}), 400
    safe_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{Path(f.filename).name}"
    out_path = BGM_DIR / safe_name
    f.save(str(out_path))
    return jsonify({'name': safe_name, 'size': out_path.stat().st_size})

# ── 前端页面 ──────────────────────────────────────────────
@app.route('/')
def index():
    try:
        initial_topics = load_topic_recommendations()
    except Exception:
        initial_topics = []
    resp = make_response(render_template('index.html', initial_topic_recommendations=initial_topics))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
