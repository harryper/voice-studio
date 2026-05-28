#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote
from flask import Flask, request, jsonify, render_template, send_from_directory, session

# ── 配置加载 ──────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

DEFAULT_CONF = {
    'password': '',
    'secret_key': '',
    'port': 9999,
    'download_root': '/root/.openclaw/workspace/public-downloads',
    'cosmic_sleep_folder': 'cosmic-sleep',
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
}
for env_key, conf_key in ENV_MAP.items():
    if os.getenv(env_key):
        CONF[conf_key] = os.getenv(env_key)

CONF['port'] = int(CONF.get('port') or 9999)

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
    # 未来添加 video：
    # 'video': {
    #     'name': '视频',
    #     'icon': '🎬',
    #     'job_dir': os.path.join(os.path.dirname(__file__), 'jobs', 'video'),
    #     'archive_dir': os.path.join(os.path.dirname(__file__), 'archive', 'video'),
    # },
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
    return data

def archive_job(job):
    """完成后归档，保留记录"""
    mode = job.get('mode', 'voice')
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
    public_endpoints = {'static', 'index', 'login', 'logout', 'api_check_auth', 'list_bgm'}
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
            'lyrics': None,
            'edited_lyrics': None,
            'status': 'pending',   # pending → lyrics_ready → generating → done
            'audio_url': None,
            'audio_path': None,
            'error': None,
            'is_instrumental': data.get('is_instrumental', False),
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
    for key in ('status', 'script', 'edited_script', 'edited_lyrics', 'voice', 'bgm', 'bgm_asset', 'audio_url', 'final_url', 'error'):
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

@app.route('/api/jobs/<job_id>/generate-lyrics', methods=['POST'])
def generate_lyrics_api(job_id):
    """Step 1: Generate lyrics from theme using MiniMax Lyrics API."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    theme = job.get('theme', '')
    if not theme:
        return jsonify({'error': '主题为空'}), 400

    try:
        job['status'] = 'generating_lyrics'
        job['error'] = None
        save_job(job)

        result = subprocess.run(
            ['python3', str(SKILL_DIR / 'scripts' / 'minimax_music.py'),
             'lyrics', '--prompt', theme],
            text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or '歌词生成失败')

        lyrics_data = json.loads(result.stdout)
        job['lyrics'] = lyrics_data.get('lyrics', '')
        job['edited_lyrics'] = None
        job['title'] = lyrics_data.get('song_title', '') or job.get('title', '')
        job['style_tags'] = lyrics_data.get('style_tags', '')
        job['status'] = 'lyrics_ready'
        save_job(job)
        return jsonify(job_response(job))
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job_response(job)), 500


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

    try:
        job['status'] = 'generating'
        job['error'] = None
        save_job(job)

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

        result = subprocess.run(cmd, text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or '音乐生成失败')

        # Upload to R2 with readable names.
        theme_slug = job_name_fragment(job, fallback='music')
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
            if lyrics_path is None:
                lyrics_path = run_dir / 'lyrics.txt'
                lyrics_path.write_text(lyrics_text, encoding='utf-8')
            lyrics_upload = subprocess.run(
                ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
                 '--file', str(lyrics_path), '--theme', 'music',
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
        save_job(job)
        return jsonify(job_response(job))
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job_response(job)), 500


@app.route('/api/jobs/<job_id>/retry-lyrics', methods=['POST'])
def retry_lyrics_api(job_id):
    """Regenerate lyrics for a music job."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    job['lyrics'] = None
    job['edited_lyrics'] = None
    job['style_tags'] = ''
    job['audio_url'] = None
    job['audio_path'] = None
    job['status'] = 'pending'
    job['error'] = None
    save_job(job)

    return generate_lyrics_api(job_id)


@app.route('/api/jobs/<job_id>/retry-music', methods=['POST'])
def retry_music_api(job_id):
    """Regenerate music from existing lyrics."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    if job.get('mode') != 'music':
        return jsonify({'error': '非音乐任务'}), 400

    job['audio_url'] = None
    job['audio_path'] = None
    job['status'] = 'lyrics_ready'
    job['error'] = None
    save_job(job)

    return generate_music_api(job_id)


@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    """列出当前活跃任务（不包括已归档）。?mode=voice|music|video"""
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
    return list_jobs_by_mode('voice')


@app.route('/api/jobs/music', methods=['GET'])
def list_music_jobs():
    return list_jobs_by_mode('music')


def list_jobs_by_mode(mode):
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
    return jsonify([job_response(job) for job in jobs])

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

    speed_val = speed if speed is not None else 0.85

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
            '--out', str(mixed_path), '--bgm-volume', str(bgm_volume if bgm_volume is not None else 0.03),
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

        run_info = _synthesize_run(job, run_id, voice, do_mix, bgm_asset, provider=data.get('provider', 'azure'), bgm_volume=bgm_volume, speed=data.get('speed', 0.85))
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
    return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
