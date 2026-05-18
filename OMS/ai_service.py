import requests

OLLAMA_URL = "http://localhost:11434/api/generate"

def get_order_summary(order_data):
    prompt = f"""
You are a senior order approval assistant.

Analyze this order and return:
1. Risk Level (Low/Medium/High)
2. Recommendation
3. Reason (short)

Order Details:
Party: {order_data['party']}
State: {order_data['state']}
Amount: {order_data['amount']}
Pending Payment: {order_data['pending_payment']}
Credit Exceeded: {order_data['credit_exceeded']}
"""

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": "qwen2.5:7b",
            "prompt": prompt,
            "stream": False
        },
        timeout=60
    )

    return response.json()["response"]