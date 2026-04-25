import uuid, json
from pathlib import Path
from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle

OUT = Path(__file__).parent / "_seq_log.txt"
OUT.write_text("", encoding="utf-8")

c = DevClient()
sid = f"insp-{uuid.uuid4().hex[:8]}"
s = SessionHandle(session_id=sid, app_id='digitorn-chat', daemon_url=c.daemon_url, workspace='')
stream = c.send_live(s, 'Dis bonjour en 3 mots.', total_timeout=60)
events = stream.events()
stream.stop(timeout=2.0)

lines = []
last_seq = 0
for i, e in enumerate(events):
    seq = int(e.get("seq", 0) or 0)
    cid = (e.get("payload") or {}).get("correlation_id", "") if isinstance(e.get("payload"), dict) else ""
    flag = " <-- DROP" if seq < last_seq else ""
    lines.append(f"[{i:3d}] seq={seq} type={e.get('type'):<30} cid={cid[:14]}{flag}")
    last_seq = seq

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {len(events)} events to {OUT}")
