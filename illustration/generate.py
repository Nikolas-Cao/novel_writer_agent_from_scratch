"""
插图生成（本地占位实现）：
将插图描述渲染为简单 SVG 文件，保存到项目 images 目录。
"""
from pathlib import Path
import re


def _safe_name(text: str) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", text.strip())
    return (t[:24] or "illustration").strip("_")


def generate_image(
    project_root: Path,
    project_id: str,
    chapter_index: int,
    image_index: int,
    prompt: str,
) -> str:
    images_dir = Path(project_root) / project_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"chapter_{chapter_index:03d}_{image_index:02d}_{_safe_name(prompt)}.svg"
    path = images_dir / filename

    caption = prompt[:40]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="576">
  <rect width="100%" height="100%" fill="#0f172a"/>
  <circle cx="180" cy="160" r="120" fill="#1d4ed8" opacity="0.45"/>
  <circle cx="760" cy="430" r="180" fill="#22c55e" opacity="0.22"/>
  <rect x="40" y="380" width="944" height="150" rx="14" fill="#111827" opacity="0.75"/>
  <text x="60" y="460" fill="#e5e7eb" font-size="34" font-family="Segoe UI, Arial">AI Illustration Placeholder</text>
  <text x="60" y="505" fill="#93c5fd" font-size="24" font-family="Segoe UI, Arial">{caption}</text>
</svg>
"""
    path.write_text(svg, encoding="utf-8")
    return f"{project_id}/images/{filename}"
