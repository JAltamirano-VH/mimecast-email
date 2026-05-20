import requests
import datetime
import csv
import pyodbc
import argparse
import concurrent.futures

# Configuration
CLIENT_ID = 'qkbzONhzzhapF1BiPrLhPg7lLL68ed6tzsoRSWt1D2voskex'
CLIENT_SECRET = 'uoAKPRHGDkZUVBlnlK3mP61FZXpHchhpZS8sZpcY0JOKXFcbItFgneOu0xnrwvkc'
BASE_URL = 'https://api.services.mimecast.com'  # Assuming US region; change if different
DB_SERVER = 'VIRPS0INF20D61'
DB_NAME = 'ITDashboard'
TABLE_NAME = 'Email'  # Assuming table name; adjust if different

INTERNAL_DOMAIN = 'valleywisehealth.org'

def get_access_token():
    url = f"{BASE_URL}/oauth/token"
    data = {
        'grant_type': 'client_credentials',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    return response.json()['access_token']

def get_messages(access_token, start_date, end_date):
    """
    Perform two searches using the v2 message-finder/search endpoint:
    1. Emails FROM the internal domain
    2. Emails TO the internal domain from external senders
    """
    url = f"{BASE_URL}/api/message-finder/search"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    all_messages = []
    date_range = f"{start_date}..{end_date}"

    print(f"Searching for emails FROM {INTERNAL_DOMAIN} between {start_date} and {end_date}...")
    # search_1 = {"from": INTERNAL_DOMAIN, "sentDate": date_range}
    search_1 = {"from": INTERNAL_DOMAIN}

    search_1_data = {
        "meta": {"pagination": {"pageSize": 100}},
        "data": [{
            "advancedTrackAndTraceOptions": 
                search_1,
            "start": start_date,
            "end": end_date
            }]
    }
    messages_1 = _execute_paginated_search(url, headers, search_1_data)
    all_messages.extend(messages_1)
    print(f"Search 1 total returned {len(messages_1)} messages")

    print(f"Searching for emails TO {INTERNAL_DOMAIN} between {start_date} and {end_date}...")
    search_2 = {"to": INTERNAL_DOMAIN}
    search_2_data = {
        "meta": {"pagination": {"pageSize": 100}},
        "data": [{
            "advancedTrackAndTraceOptions": search_2,
            "start": start_date,
            "end": end_date
            }]
    }
    messages_2 = _execute_paginated_search(url, headers, search_2_data)
    all_messages.extend(messages_2)
    print(f"Search 2 total returned {len(messages_2)} messages")

    return all_messages


def deduplicate_messages(messages):
    message_dict = {}
    for msg in messages:
        msg_id = msg.get('id') or msg.get('messageId')
        if msg_id:
            message_dict[msg_id] = msg
    return list(message_dict.values())


def _execute_paginated_search(url, headers, search_data):
    """Execute a search with pagination support."""
    messages = []
    page_token = None
    
    while True:
        # Set page token if it exists from previous iteration
        if page_token:
            search_data["meta"]["pagination"]["pageToken"] = page_token
        else:
            search_data["meta"]["pagination"].pop("pageToken", None)
        
        try:
            response = requests.post(url, headers=headers, json=search_data)
            response.raise_for_status()
            result = response.json()
            
            # DEBUG: Check for errors and response structure
            if 'fail' in result and result['fail']:
                print(f"DEBUG - API Error: {result['fail']}")
            print(f"DEBUG - Response meta: {result.get('meta', {})}")
            print(f"DEBUG - Data length: {len(result.get('data', []))}")
            
            # Extract messages from response
            if 'data' in result and result['data']:
                for data_item in result['data']:
                    # Check various possible field names for messages
                    if 'messages' in data_item:
                        messages.extend(data_item['messages'])
                    elif 'emails' in data_item:
                        messages.extend(data_item['emails'])
                    elif 'trackedEmails' in data_item:
                        messages.extend(data_item['trackedEmails'])
            
            # Check for next page
            page_token = result.get('meta', {}).get('pagination', {}).get('pageToken')
            if not page_token:
                break
        except requests.exceptions.RequestException as e:
            print(f"Error during search: {e}")
            break
    
    return messages

def process_messages(messages):
    inbound_count = 0
    outbound_count = 0
    email_list = []

    for msg in messages:
        # Determine direction based on whether sender is internal or external
        sender_address = msg.get('fromEnv', {}).get('emailAddress', '')
        # Build a list of recipient email addresses (handle multiple recipients)
        recipient_addresses = [r.get('emailAddress', '') for r in (msg.get('to') or [])]
        # Fallback to empty string if there are no recipients
        recipient_str = '; '.join([r for r in recipient_addresses if r])

        sender_internal = INTERNAL_DOMAIN in sender_address
        # True if any recipient is internal
        recipient_internal = any(INTERNAL_DOMAIN in r for r in recipient_addresses)
        # print(f"Sender Internal: {sender_internal}, Sender: {sender_address}, Recipient Internal: {recipient_internal},Recipient: {recipient_address}")

        # Classify as inbound (external to internal) or outbound (internal to external)
        # For the second search (TO internal domain), we only want external senders
        if not sender_internal and recipient_internal:
            # print(f"Inbound: {sender_address} -> {recipient_str}")
            direction = 'inbound'
            inbound_count += 1
        elif sender_internal and not recipient_internal:
            # print(f"Outbound: {sender_address} -> {recipient_str}")
            direction = 'outbound'
            outbound_count += 1
        else:
            # Internal to internal or other - skip for second search
            # print(f"Skipping message with sender: {sender_address} and recipient: {recipient_str} (not classified as inbound or outbound)")
            continue

        email_list.append({
            'date': msg.get('sent') or msg.get('received'),
            'sender': sender_address,
            'recipient': recipient_str,
            'subject': msg.get('subject'),
            'direction': direction,
            'messageid': msg.get('messageId') or msg.get('id') or ''
        })

    return inbound_count, outbound_count, email_list

def export_to_csv(email_list, filename='emails.csv'):
    if not email_list:
        print("No emails to export.")
        return

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['date', 'sender', 'recipient', 'subject', 'direction', 'messageid'])
        writer.writeheader()
        writer.writerows(email_list)
    print(f"Emails exported to {filename}")

def insert_to_sql(date, inbound_count, outbound_count, report_type='Mimecast', stg_added_dtm=None):
    if stg_added_dtm is None:
        stg_added_dtm = datetime.datetime.now()

    conn_str = f'DRIVER={{SQL Server}};SERVER={DB_SERVER};DATABASE={DB_NAME};Trusted_Connection=yes;'
    try:
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()

        # Insert inbound
        cursor.execute(
            f"INSERT INTO {TABLE_NAME} (Date, Item, Value, ReportType, stg_AddedDTM) VALUES (?, 'Inbound', ?, ?, ?)",
            date,
            inbound_count,
            report_type,
            stg_added_dtm,
        )
        # Insert outbound
        cursor.execute(
            f"INSERT INTO {TABLE_NAME} (Date, Item, Value, ReportType, stg_AddedDTM) VALUES (?, 'Outbound', ?, ?, ?)",
            date,
            outbound_count,
            report_type,
            stg_added_dtm,
        )

        conn.commit()
        print("Data inserted into SQL table.")
    except Exception as e:
        print(f"Error inserting to SQL: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

def get_interval_messages(access_token, start_dt, end_dt):
    start_date = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_date = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f"Checking interval {start_date}..{end_date}")
    messages = get_messages(access_token, start_date, end_date)
    print(f"Interval returned {len(messages)} messages for {start_date}..{end_date}")
    return messages


def get_30_min_intervals(date):
    intervals = []
    current = datetime.datetime.combine(date, datetime.time(0, 0, 0))
    final_end = datetime.datetime.combine(date, datetime.time(0, 59, 59))

    while current <= final_end:
        interval_end = current + datetime.timedelta(minutes=29, seconds=59)
        if interval_end > final_end:
            interval_end = final_end
        intervals.append((current, interval_end))
        current = current + datetime.timedelta(minutes=1)

    return intervals


def main():
    parser = argparse.ArgumentParser(description='Retrieve Mimecast email stats for previous day.')
    parser.add_argument('--csv', action='store_true', help='Export emails to CSV file')
    args = parser.parse_args()

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    date_str = yesterday.strftime('%Y-%m-%d')

    print(f"Retrieving emails for {date_str} in 30-minute intervals")

    try:
        token = get_access_token()
        print("Authenticated successfully.")

        all_messages = []
        intervals = get_30_min_intervals(yesterday)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(get_interval_messages, token, start_dt, end_dt) for start_dt, end_dt in intervals]
            for future in concurrent.futures.as_completed(futures):
                try:
                    interval_messages = future.result()
                    all_messages.extend(interval_messages)
                except Exception as e:
                    print(f"Error retrieving interval messages: {e}")

        unique_messages = deduplicate_messages(all_messages)
        print(f"Total retrieved across all intervals before dedupe: {len(all_messages)}")
        print(f"Total unique retrieved across all intervals: {len(unique_messages)}")

        with open('debug_messages.json', 'w', encoding='utf-8') as f:
            import json
            json.dump(unique_messages, f, ensure_ascii=False, indent=4)

        inbound, outbound, email_list = process_messages(unique_messages)
        print(f"Total inbound: {inbound}, Total outbound: {outbound}")

        if args.csv:
            export_to_csv(email_list)

        insert_to_sql(date_str, inbound, outbound)

    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    main()
    
# </content> <parameter name="filePath">c:\repos\jaltamirano\scripts\mimecast_email_stats.py