import requests
import re

resp = requests.get('http://127.0.0.1:5000/')
print('Index:', resp.status_code)
print('Length:', len(resp.text))
# Check for chatbot.js
scripts = re.findall(r'<script src="([^"]+)"></script>', resp.text)
print('Scripts:', scripts)

# Check for chatbot.js in inline scripts
if 'chatbot.js' in resp.text:
    print('chatbot.js found in HTML')
else:
    print('chatbot.js NOT found in HTML')