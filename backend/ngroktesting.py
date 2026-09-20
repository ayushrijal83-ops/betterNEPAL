from openai import OpenAI

# Replace with your actual ngrok URL (remember to append /v1)
NGROK_URL = "https://abruptly-magnesium-helmet.ngrok-free.dev/v1"

client = OpenAI(
    base_url=NGROK_URL,
    api_key="sk-betternepal-local",
    default_headers={"ngrok-skip-browser-warning": "true"}
)

print("Testing connection to HP Victus...")
try:
    response = client.chat.completions.create(
        model="qwen2.5:3b",
        messages=[
            {"role": "system", "content": "You analyze civic issues. Output JSON with 'category' and 'severity'."},
            {"role": "user", "content": "Large pothole near Maitighar Mandala causing severe bottleneck."}
        ],
        response_format={"type": "json_object"},
        timeout=20
    )
    print("\n--- Success! Response from HP Victus ---")
    print(response.choices[0].message.content)
except Exception as e:
    print(f"\nConnection failed: {e}")