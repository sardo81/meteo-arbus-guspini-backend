"""Build-time patch for the known a5a9c77 Cron, not executed in production."""
import ast, pathlib, subprocess
p=pathlib.Path('cron_automation.py')
assert subprocess.check_output(['git','hash-object',str(p)],text=True).strip()=='eaca3901e3234e943c2677b5de769faf87a7bd82', 'Unexpected Cron baseline'
s=p.read_text();t=ast.parse(s)
m=next(n for n in t.body if isinstance(n,ast.FunctionDef) and n.name=='main')
v=next(n for n in ast.walk(m) if isinstance(n,ast.FunctionDef) and n.name=='verify')
lines=s.splitlines(keepends=True)
replacement='''        def verify(hour: int):
            nonlocal verification_report
            from station_verification import verify_independent
            value = set_time(hour)
            # Same station backend, independent validation, no immediate fallback.
            payload = http_json(f"{backend}/stations", timeout=150)
            verification_report = verify_independent(page, value, payload, run_key)
'''
s=''.join(lines[:v.lineno-1])+replacement+''.join(lines[v.end_lineno:])
anchor='    with sync_playwright() as p:\n        browser = p.chromium.launch('
assert s.count(anchor)==1
s=s.replace(anchor,'    verification_report = None\n\n'+anchor)
old='''    new_state["automationMeta"] = meta

    new_state = merge_meta(
        new_state,
        run_key,
    )
'''
assert s.count(old)==1
s=s.replace(old,'''    if verification_report is not None:
        from station_verification import finalize_verification_state
        fresh_state = http_json(f"{backend}/automation/state").get("state")
        if not isinstance(fresh_state, dict):
            raise RuntimeError("VERIFY_CLOUD_STATE_INVALID")
        new_state = finalize_verification_state(
            state, new_state, fresh_state, verification_report, run_key, merge_meta
        )
    else:
        new_state["automationMeta"] = meta
        new_state = merge_meta(new_state, run_key)
''')
anchor='''    print(
        f"DONE {run_key} "'''
assert s.count(anchor)==1
s=s.replace(anchor,'''    if verification_report is not None and verification_report["status"] != "complete":
        label = "DONE_PARTIAL" if verification_report["status"] == "partial" else "VERIFY_UNAVAILABLE"
        print(
            f"{label} {run_key} "
            f"history={len(new_state.get('history', []))} "
            f"archive={len(new_state.get('archive', []))} "
            f"verified={verification_report['verifiedPlaces']} "
            f"cloud={result.get('status', 'ok')}", flush=True
        )
        return 0 if verification_report["status"] == "partial" else 1

'''+anchor)
ast.parse(s)
p.write_text(s)
print('PATCH_PASS independent_stations=True no_forecast_model_changes=True')
