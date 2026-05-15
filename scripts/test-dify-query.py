import json
import sys
from urllib.request import Request, urlopen

question = "Come posso usare Brain-Home per gestire una knowledge base?"
if len(sys.argv) > 1:
    question = " ".join(sys.argv[1:])

payload = json.dumps({"question": question}).encode("utf-8")
req = Request("http://100.87.153.12:8000/proxy", data=payload, headers={"Content-Type": "application/json"})

with urlopen(req, timeout=10) as response:
    result = json.load(response)
    print(json.dumps(result, indent=2, ensure_ascii=False))
