import requests
import re

resp = requests.get('http://127.0.0.1:5000/pages/citizen/dashboard.html')
print('Status:', resp.status_code)
print('Has chatbot.js:', 'chatbot.js' in resp.text)
print('Has api.js:', 'api.js' in resp.text)
print('Script order check:')
scripts = re.findall(r'<script src="([^"]+)"></script>', resp.text)
for i, s in enumerate(scripts):
    print(f'  {i}: {s}')