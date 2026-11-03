"""运行五章自写样稿；默认离线，可重复运行验证缓存。"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["fake", "deepseek"], default="fake")
    parser.add_argument("--db", type=Path, default=Path("data/stage2_demo.db"))
    parser.add_argument("--env-file", type=Path, help="可指定独立配置，便于离线测试隔离")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    db = args.db.resolve()
    env_args = ["--env-file", str(args.env_file.resolve())] if args.env_file else []

    def run(arguments):
        result = subprocess.run(
            [sys.executable, "-m", "easy_writing", *arguments, "--db", str(db), *env_args],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
        )
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode:
            raise SystemExit(result.returncode)
        return json.loads(result.stdout)

    project = run(["project", "create", f"五章样稿-{args.provider}"])
    imports = []
    for number in range(1, 6):
        path = root / "examples" / "stage2" / f"{number:02d}"
        command = ["ingest", str(path.with_suffix(".txt")), "--project", project["id"],
                   "--chapter", str(number), "--provider", args.provider]
        if args.provider == "fake":
            command += ["--fixture", str(path.with_suffix(".json"))]
        imports.append(run(command))
    chapters = run(["chapters", "--project", project["id"]])
    facts = run(["knowledge", "--project", project["id"], "--kind", "fact"])
    checked = 0
    for fact in facts:
        sources = run(["source", "--kind", "fact", "--id", fact["id"]])
        assert sources, "事实缺少原文证据"
        for source in sources:
            assert source["paragraph_text"][source["quote_start"]:source["quote_end"]] == source["quote"]
            checked += 1
    print(json.dumps({
        "provider": args.provider, "database": str(db), "project_id": project["id"],
        "chapters": len(chapters), "current_chapters": sum(c["knowledge_is_current"] for c in chapters),
        "facts": len(facts), "evidence_checked": checked,
        "cached_chapters": sum(item["cached"] for item in imports),
        "model_calls": sum(item["model_calls"] for item in imports), "imports": imports,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
