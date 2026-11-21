"""Command-line interface to the pipeline.

Run:  python src/ask.py "Why did success rate drop last week?" --role central_analyst
"""
import argparse

from pipeline import ask


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--role", default="central_analyst")
    args = parser.parse_args()

    out = ask(args.question, args.role)
    print(f"STATUS: {out['status']}  ({out['total_s']}s)")
    if out["message"]:
        print("MESSAGE:", out["message"])
    if out.get("plan"):
        print("INTERPRETATION:", out["plan"].get("interpretation"))
    if out["narrative"]:
        print("\nHEADLINE:", out["narrative"].get("headline"))
    print("\nFACTS (computed by code):")
    for f in out["facts"]:
        print(f"  [{f['id']}] {f['text']}")
    if out["narrative"]:
        print("\nHYPOTHESES (not proven):")
        for h in out["narrative"].get("hypotheses", []):
            flag = "  [UNVERIFIED NUMBER]" if h.get("unverified") else ""
            print(f"  - ({h.get('confidence')}) {h['text']}  evidence={h.get('evidence')}{flag}")
        print("\nRECOMMENDATIONS:")
        for r in out["narrative"].get("recommendations", []):
            print(f"  - {r['text']}  evidence={r.get('evidence')}")
    print("\nCAVEATS:")
    for c in out["caveats"]:
        print("  -", c)
    print("\nQUERIES RUN:")
    for q in out["queries"]:
        print(f"  [{q['id']}] ({q.get('source')}) ok={q.get('ok', True)} rows={q.get('rows')} {q.get('errors') or ''}")
    for c in out["llm_calls"]:
        print(f"LLM {c['stage']}: model={c['model']} tokens={c['input_tokens']}+{c['output_tokens']} {c['latency_s']}s")
    if out["number_check"]:
        print("NUMBER CHECK PROBLEMS:", out["number_check"])


if __name__ == "__main__":
    main()
