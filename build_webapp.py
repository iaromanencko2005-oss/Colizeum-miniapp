"""Сборка мини-приложения для старых телефонов.

Исходник — webapp_src/index.html (правим его). Скрипт переводит основной
JavaScript в формат, который понимают встроенные браузеры Android с 2017 года
(Chrome 58+), и кладёт результат в webapp/index.html — его и заливаем на GitHub."""
import re, subprocess, sys, tempfile, os
ESBUILD = os.getenv("ESBUILD", "/home/claude/.npm-global/lib/node_modules/tsx/node_modules/esbuild/bin/esbuild")
src = open("webapp_src/index.html", encoding="utf-8").read()
start = src.index("<script>\nconst tg")
end = src.index("</script>", start)
js = src[start + len("<script>\n"):end]
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
    f.write(js)
out = subprocess.run([ESBUILD, f.name, "--target=chrome58", "--charset=utf8", "--log-level=warning"],
                     capture_output=True, text=True)
if out.returncode:
    sys.exit(out.stderr)
built = src[:start] + "<script>\n" + out.stdout + src[end:]
open("webapp/index.html", "w", encoding="utf-8").write(built)
print("ok:", len(js), "->", len(out.stdout), "символов")
