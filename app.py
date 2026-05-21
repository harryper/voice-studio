#!/usr/bin/env python3
import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
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

# ── 状态文件 ──────────────────────────────────────────────
JOBS_DIR = os.path.join(os.path.dirname(__file__), 'jobs')
os.makedirs(JOBS_DIR, exist_ok=True)

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), 'archive')
os.makedirs(ARCHIVE_DIR, exist_ok=True)

def job_path(job_id):
    return os.path.join(JOBS_DIR, f'{job_id}.json')

def save_job(job):
    job['updated_at'] = datetime.now().isoformat()
    with open(job_path(job['id']), 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False, indent=2)

def load_job(job_id):
    p = job_path(job_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)

def archive_job(job):
    """完成后归档，保留记录"""
    dest = os.path.join(ARCHIVE_DIR, f'{job["id"]}.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.remove(job_path(job['id']))

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
    public_endpoints = {'static', 'index', 'login', 'logout', 'api_check_auth'}
    if request.endpoint in public_endpoints:
        return
    if session.get('authenticated') is not True:
        return jsonify({'error': '未登录'}), 401

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
            'audio_url': None,
            'final_url': None,
            'error': None,
            'created_at': datetime.now().isoformat(),
            'bgm': data.get('bgm', True),
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
        }
    else:
        return jsonify({'error': 'mode 必须是 theme 或 script'}), 400

    save_job(job)
    return jsonify({'job_id': job_id, 'job': job})

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify(job)

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
    for key in ('status', 'script', 'audio_url', 'final_url', 'error'):
        if key in data:
            job[key] = data[key]

    save_job(job)
    return jsonify(job)

@app.route('/api/jobs/<job_id>/approve', methods=['POST'])
def approve_job(job_id):
    """用户审批通过，进入 TTS"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    # 使用编辑后的文稿（如果有）
    script = job.get('edited_script') or job.get('script')
    if not script:
        return jsonify({'error': '文稿为空，无法生成'}), 400

    job['status'] = 'tts'
    save_job(job)
    return jsonify(job)

@app.route('/api/jobs/<job_id>/retry', methods=['POST'])
def retry_job(job_id):
    """重新创作（仅 theme 模式）"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    job['status'] = 'pending'
    job['script'] = None
    job['edited_script'] = None
    job['error'] = None
    save_job(job)
    return jsonify(job)

@app.route('/api/jobs/<job_id>/archive', methods=['POST'])
def archive_job_api(job_id):
    """归档任务（主 session 在完成后调用）"""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404
    archive_job(job)
    return jsonify({'ok': True})

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    """列出当前活跃任务（不包括已归档）"""
    jobs = []
    for fname in os.listdir(JOBS_DIR):
        if fname.endswith('.json'):
            with open(os.path.join(JOBS_DIR, fname), encoding='utf-8') as f:
                jobs.append(json.load(f))
    jobs.sort(key=lambda x: x['created_at'], reverse=True)
    return jsonify(jobs)

def safe_name(value):
    value = value or 'voice'
    value = re.sub(r'[^A-Za-z0-9._-]+', '-', value.strip())[:80].strip('-')
    return value or 'voice'

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

@app.route('/api/jobs/<job_id>/process-tts', methods=['POST'])
def process_tts(job_id):
    """Convert an approved script into narration, optional BGM mix, and public URL."""
    job = load_job(job_id)
    if not job:
        return jsonify({'error': '任务不存在'}), 404

    script = (job.get('edited_script') or job.get('script') or '').strip()
    if not script:
        return jsonify({'error': '文稿为空，无法生成'}), 400

    try:
        run_dir = RUNS_DIR / job_id
        run_dir.mkdir(parents=True, exist_ok=True)
        script_path = run_dir / 'script.txt'
        voice_path = run_dir / 'voice.mp3'
        mixed_path = run_dir / 'final.mp3'
        script_path.write_text(script, encoding='utf-8')

        job['status'] = 'tts'
        job['error'] = None
        save_job(job)

        run_cmd([
            'python3', str(SKILL_DIR / 'scripts' / 'azure_tts.py'),
            '--file', str(script_path),
            '--style', 'calm',
            '--rate=-10%',
            '--pause-ms', '800',
            '-o', str(voice_path),
        ])
        job['audio_path'] = str(voice_path)
        save_job(job)

        final_source = voice_path
        if job.get('bgm', True):
            job['status'] = 'mixing'
            save_job(job)
            run_cmd([
                'python3', str(SKILL_DIR / 'scripts' / 'mix_with_bgm.py'),
                '--voice', str(voice_path),
                '--bgm', str(SKILL_DIR / 'assets' / 'bgm_default.mp3'),
                '--out', str(mixed_path),
                '--bgm-volume', '0.03',
            ])
            final_source = mixed_path

        name_base = safe_name(job.get('theme') or f'custom-{job_id}')
        public_name = f'{datetime.now().strftime("%Y%m%d-%H%M%S")}-{name_base}.mp3'
        publish_out = run_cmd([
            'python3', str(SKILL_DIR / 'scripts' / 'publish_download.py'),
            '--file', str(final_source),
            '--folder', COSMIC_FOLDER,
            '--name', public_name,
        ])
        final_url = publish_out.splitlines()[-1].strip()

        job['status'] = 'done'
        job['final_url'] = final_url
        job['final_path'] = str(final_source)
        save_job(job)
        return jsonify(job)
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job), 500

# ── 音频文件服务 ──────────────────────────────────────────
@app.route('/download/<path:filename>')
def download(filename):
    return send_from_directory(DOWNLOAD_ROOT, filename)

# ── 前端页面 ──────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
