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

# ── 状态文件 ──────────────────────────────────────────────
JOBS_DIR = os.path.join(os.path.dirname(__file__), 'jobs')
os.makedirs(JOBS_DIR, exist_ok=True)

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
    cleanup_job_outputs(job)
    source = Path(job_path(job['id']))
    if not source.exists():
        return
    deleted_dir = Path(ARCHIVE_DIR) / 'deleted'
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
    for key in ('status', 'script', 'edited_script', 'voice', 'bgm', 'bgm_asset', 'audio_url', 'final_url', 'error'):
        if key in data:
            job[key] = data[key]

    save_job(job)
    return jsonify(job)

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
    return jsonify(job)

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
    return jsonify(job)

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
        return jsonify(job)
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job), 500


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

        # Upload to R2
        theme_slug = safe_name(job.get('title') or job.get('theme') or 'music')
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        upload_result = subprocess.run(
            ['python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
             '--file', str(output_path), '--theme', 'music',
             '--name', ts + '-' + job['id'] + '-' + theme_slug + '.mp3'],
            text=True, capture_output=True, cwd=str(SKILL_DIR), timeout=60,
        )
        if upload_result.returncode == 0:
            audio_url = upload_result.stdout.strip().splitlines()[-1].strip()
        else:
            audio_url = None

        job['audio_path'] = str(output_path)
        job['audio_url'] = audio_url
        job['status'] = 'done'
        save_job(job)
        return jsonify(job)
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job), 500


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
    """列出当前活跃任务（不包括已归档）"""
    jobs = []
    for fname in os.listdir(JOBS_DIR):
        if fname.endswith('.json'):
            try:
                with open(os.path.join(JOBS_DIR, fname), encoding='utf-8') as f:
                    jobs.append(json.load(f))
            except json.JSONDecodeError:
                # A single corrupt job file should not take down the whole UI.
                continue
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

    name_base = safe_name(job.get('theme') or f'custom-{job["id"]}')
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    theme_slug = safe_name(job.get('theme') or 'direct-script')

    # Upload script
    script_url = run_cmd([
        'python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
        '--file', str(script_path), '--theme', theme_slug,
        '--name', f'{ts}-{run_id}-script.txt',
    ]).splitlines()[-1].strip()

    # Upload voice
    voice_url = run_cmd([
        'python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
        '--file', str(voice_path), '--theme', theme_slug,
        '--name', f'{ts}-{run_id}-voice.mp3',
    ]).splitlines()[-1].strip()

    # Mix + upload if needed
    if do_mix and bgm_path.exists():
        run_cmd([
            'python3', str(SKILL_DIR / 'scripts' / 'mix_with_bgm.py'),
            '--voice', str(voice_path), '--bgm', str(bgm_path),
            '--out', str(mixed_path), '--bgm-volume', str(bgm_volume if bgm_volume is not None else 0.03),
        ])
        final_source = mixed_path
    else:
        final_source = voice_path

    final_url = run_cmd([
        'python3', str(SKILL_DIR / 'scripts' / 'upload_to_oss.py'),
        '--file', str(final_source), '--theme', theme_slug,
        '--name', f'{ts}-{run_id}-final.mp3',
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
            return jsonify(job)

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
        return jsonify(job)
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job), 500


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
            return jsonify(job)

        job['status'] = 'tts'
        job['error'] = None
        save_job(job)

        run_info = _synthesize_run(job, run_id, voice, do_mix, bgm_asset, provider=data.get('provider', 'azure'), bgm_volume=bgm_volume, speed=data.get('speed', 0.85))
        job['voice_runs'].append(run_info)
        job['final_url'] = run_info['final_url']
        job['voice_url'] = run_info['voice_url']
        job['status'] = 'done'
        save_job(job)
        return jsonify(job)
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        save_job(job)
        return jsonify(job), 500

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
