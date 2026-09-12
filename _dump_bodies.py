import sys, sqlite3
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
conn = sqlite3.connect("monitor.db")
conn.row_factory = sqlite3.Row
ids = [1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 26, 28, 29, 37, 48, 49, 50, 51]
for r in conn.execute(
    "select id, post_number, clean_text from listings where id in (%s) order by id" % ",".join(map(str, ids))
):
    print("=== listing #%s post #%s ===" % (r["id"], r["post_number"]))
    print("--- clean_text ---")
    print(r["clean_text"])