import psycopg2
import json
import requests

webhook_url = '' # EventStream URL
auth_token = ''  # Authentication token from Event Stream Token Credential Type

# Database connection details
db_config = {
    "host": "db.example.com",
    "port": 5432,
    "dbname": "awx",
    "user": "awx",
    "password": "password"
}

try:
    # Connect to the PostgreSQL database
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor()

    # Execute query to check if the instance is in recovery mode
    cur.execute("SELECT pg_is_in_recovery();")
    in_recovery = cur.fetchone()[0]

    # Store result in JSON format
    data = {
        "in_recovery": in_recovery
    }

    # Print JSON result
    print(json.dumps(data, indent=4))

    # Clean up
    cur.close()
    conn.close()

except Exception as e:
    error_result = {
        "error": str(e)
    }
    print(json.dumps(error_result, indent=4))

headers = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {auth_token}'  # Authorization header
}

response = requests.post(webhook_url, json=data, headers=headers, verify=False) # ssl_verify false

print(f"Status Code: {response.status_code}")
print(f"Response: {response.text}")