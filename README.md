# voice-studio

一键创作 **~20分钟** 沉浸式助眠音频（睡前故事 / 叙事 / 氛围音景）的 Web 项目。

**核心流程：** 主题 → AI 写稿（NVIDIA qwen3.5-397b）→ 人工审核 → Web UI 一键生成音频 + 混入 BGM → 发布公网链接。

---

## 30 秒极速接入（新机器）

```bash
# 1. 克隆项目
git clone https://github.com/harryper/voice-studio.git
cd voice-studio

# 2. 配置密钥（任选一种）
cp config.example.json config.json
# 编辑 config.json，填入密码和密钥

# 或者直接用环境变量
export VOICE_STUDIO_PASSWORD=你的密码
export VOICE_STUDIO_SECRET_KEY=你的密钥

# 3. Azure TTS 密钥
echo "你的Azure密钥" > scripts/azure_speech_key.txt
chmod 600 scripts/azure_speech_key.txt

# MiniMax 密钥（备用）
echo "你的MiniMax密钥" > scripts/minimax_api_key.txt
chmod 600 scripts/minimax_api_key.txt

# 4. 安装依赖
pip install -r requirements.txt

# 5. 启动
python3 app.py

# 6. 打开浏览器
open http://127.0.0.1:9999/
```

---

## 工作流

```
主题/文稿 → Web Job → AI 自动写稿（后台）→ ready 状态 → 人工审核 → TTS 生成 → 混入 BGM → 发布公网链接
```

1. 在 Web UI 创建主题任务或粘贴文稿
2. 后台自动触发 NVIDIA qwen3.5-397b 生成中文旁白稿，写入 `runs/<job_id>/script.txt`，状态更新为 `ready`
3. 在 Web UI 审核 / 编辑文稿
4. 选择音色、语速、混音音量，点击「生成」
5. 完成后自动发布公网 MP3 链接

---

## 旁白角色：老波

文稿以 **老波** 第一人称贯穿始终。

**写作风格**（详见 `reference-style.md`）：
- 沉浸式助眠音频，非知识科普文章
- 第二人称感官场景（你躺在床上、黑暗、夜声）
- 前 60-90 秒内必须让听者明确主题
- 低认知负荷；知识点是垫脚石，不是课堂讲义
- 纯散文，自然分段；无标题、无标签、无 markdown
- 品牌落款：**"我是老波，咱们在梦中的平行宇宙继续聊。"** 或 **"我是老波，祝你晚安。"**

---

## 目录结构

```
voice-studio/
├── app.py                  # Flask 后端（全部 API）
├── templates/index.html    # Web UI（全前端逻辑）
├── docker-compose.yml      # 容器编排
├── scripts/
│   ├── azure_tts.py        # Azure TTS（主用）
│   ├── minimax_tts.py      # MiniMax TTS（备用）
│   ├── mix_with_bgm.py    # 混音
│   ├── upload_to_oss.py    # 上传 OSS
│   └── publish_download.py # 发布公网链接
├── jobs/                  # Web Job 状态文件（JSON）
├── runs/                  # 生成物（脚本、音频、分段文件）
├── assets/
│   └── bgm_default.mp3    # 默认氛围 BGM
└── reference-style.md      # 写作风格参考
```

---

## 音色与配置

### Web UI 可调参数

| 参数 | 选项 / 默认值 |
|------|-------------|
| **服务商** | Azure TTS（主用）/ MiniMax TTS（备用） |
| **Azure 音色** | 云泽 / 云希 / 云健 / 云扬 / 云枫 / 晓晓 / 晓依 / 晓辰 |
| **MiniMax 音色** | Deep Voice（低沉男声）/ 温婉柔和 |
| **语速** | 0.75x / 0.80x / **0.85x（默认）** / 0.90x / 1.00x |
| **混音音量** | 滑块 0~20%，默认 6% |

### Azure TTS（主用）

| 参数 | 值 |
|------|---|
| Voice | `zh-CN-YunzeNeural`（云泽，中年男声）|
| Style | `calm` |
| Rate | `-10%` |
| Region | `eastasia` |
| 免费额度 | 50 万字符 / 月 |

### MiniMax TTS（备用）

| 参数 | 值 |
|------|---|
| Model | `speech-2.8-hd` |
| 可用音色 | Deep Voice / 温婉柔和（Gentleman 已下线）|
| Speed | `0.85` |
| 每日额度 | 11000 字 |

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `VOICE_STUDIO_PASSWORD` | Web UI 登录密码 |
| `VOICE_STUDIO_SECRET_KEY` | API 签名密钥 |
| `VOICE_STUDIO_PORT` | 端口（默认 9999）|
| `VOICE_STUDIO_DOWNLOAD_ROOT` | 公网下载根目录 |
| `VOICE_STUDIO_COSMIC_FOLDER` | cosmic-sleep 发布文件夹 |

---

## 常见问题

**Q: 页面打不开**
```bash
# 检查进程是否在跑
lsof -i:9999

# 重启
kill $(lsof -ti:9999); python3 app.py
```

**Q: TTS 生成失败**
- 检查 `scripts/azure_speech_key.txt` 是否存在且权限正确（600）
- 检查 Azure 余额是否充足
- 尝试切 MiniMax 备用

**Q: 内存不足（OOM）**
- 减少文稿长度或分段生成
- MiniMax 备用链路对长文本更稳定

**Q: 想改 BGM**
Web UI 内支持上传自定义 BGM（MP3/WAV 等），也可直接在 `assets/` 目录替换 `bgm_default.mp3`。

---

## 衍生项目

- **voice-studio-scripts** — https://github.com/harryper/voice-studio-scripts（独立脚本合集）