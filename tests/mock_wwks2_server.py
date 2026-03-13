import asyncio
import websockets
import os
import sys

clients = set()

WWKS2_MESSAGES_FILE = "wwks2_messages.txt"

async def ws_handler(websocket):
    print(f"\n[+] Client connected: {websocket.remote_address}")
    clients.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        clients.remove(websocket)
        print(f"\n[-] Client disconnected: {websocket.remote_address}")

async def main():
    host = "0.0.0.0"
    port = 6050
    
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), WWKS2_MESSAGES_FILE)
    if not os.path.exists(file_path):
        print(f"Error: Cannot find {file_path}")
        return
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return

    print(f"Loaded {len(lines)} messages from {WWKS2_MESSAGES_FILE}.")
    
    async with websockets.serve(ws_handler, host, port):
        print(f"WebSocket server started on ws://{host}:{port}")
        print("Waiting for clients to connect...")
        print("\nCommands:")
        print("  [Enter] - Send the next message to all connected clients")
        print("  all     - Send all remaining messages (1 per second delay)")
        print("  q       - Quit")
        print("-" * 50)
        
        i = 0
        while i < len(lines):
            cmd = await asyncio.to_thread(input, f"[{i+1}/{len(lines)}] Command > ")
            cmd = cmd.strip().lower()
            
            if cmd == 'q' or cmd == 'quit':
                break
            elif cmd == 'all':
                for j in range(i, len(lines)):
                    if not clients:
                        print("No clients connected to send message to.")
                    print(f"Sending message {j+1} to {len(clients)} clients...")
                    websockets.broadcast(clients, lines[j])
                    await asyncio.sleep(1.0) # sleep 1s between messages
                print("Finished sending all messages.")
                break
            else:
                if not clients:
                    print("Warning: No clients currently connected.")
                print(f"Sending message {i+1} to {len(clients)} clients...")
                websockets.broadcast(clients, lines[i])
                i += 1

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting.")
