import os, re, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}
os.makedirs("out", exist_ok=True)
pages = ["https://www.gov.pl/web/kowr/ot-poznan", "https://www.gov.pl/web/kowr/ot-bydgoszcz", "https://www.gov.pl/web/kowr/ot-wroclaw"]
atts = ["c89f7110-749c-4085-a3bf-ee83b0f3f7b1", "26a44476-def5-4267-9229-dd78cdbe1e54", "8e374800-5cb6-4208-989e-248244fa5b85", "4d1fd9a1-b989-47d9-801b-fc4c7666b3c5", "2aa7fd87-827b-45d9-a99a-bb8258ceadea", "14834f44-a919-49ae-8129-6b40b713fad8"]
for u in pages:
    r = requests.get(u, headers=UA, timeout=60)
    name = u.rsplit("/", 1)[1]
    open(f"out/{name}.html", "wb").write(r.content)
    links = sorted(set(re.findall(r'href="(/web/kowr[^"#?]*|/attachment/[0-9a-f-]+)"', r.text)))
    print(u, r.status_code, len(r.content), "links:", len(links))
    for l in links:
        if re.search(r"przetarg|ogloszen|dzierzaw|sprzedaz|wykaz|harmonogram|attachment", l): print("   ", l)
for a in atts:
    r = requests.get(f"https://www.gov.pl/attachment/{a}", headers=UA, timeout=60)
    cd = r.headers.get("Content-Disposition", ""); ct = r.headers.get("Content-Type", "")
    ext = ".pdf" if "pdf" in ct else ".docx" if "word" in ct or "officedocument" in ct else ".bin"
    open(f"out/{a}{ext}", "wb").write(r.content)
    print("ATT", a, r.status_code, ct, len(r.content), cd[:120])
