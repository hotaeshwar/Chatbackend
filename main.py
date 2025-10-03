from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Dict, List, Set
from datetime import datetime
import json
import os
import shutil
from pathlib import Path
import uuid

app = FastAPI(title="Bid Chat API - Production")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chatapp.buildingindiadigital.com",
        "http://chatapp.buildingindiadigital.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.message_history: Dict[str, List[dict]] = {"public": []}
        self.user_rooms: Dict[str, Set[str]] = {}
        self.room_members: Dict[str, Set[str]] = {"public": set()}
        self.room_metadata: Dict[str, dict] = {}
        self.user_login_times: Dict[str, datetime] = {}
    
    async def connect(self, user_id: str, websocket: WebSocket):
        self.active_connections[user_id] = websocket
        
        if user_id not in self.user_login_times:
            self.user_login_times[user_id] = datetime.now()
            print(f"First time login recorded for '{user_id}'")
        
        if user_id not in self.user_rooms:
            self.user_rooms[user_id] = set()
        self.user_rooms[user_id].add("public")
        self.room_members["public"].add(user_id)
        
        print(f"'{user_id}' connected. Online: {len(self.active_connections)}")
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            
            if user_id in self.user_rooms:
                for room_id in self.user_rooms[user_id]:
                    if room_id in self.room_members:
                        self.room_members[room_id].discard(user_id)
                del self.user_rooms[user_id]
            
            print(f"'{user_id}' disconnected. Online: {len(self.active_connections)}")
    
    def create_room(self, room_type: str, members: List[str], room_name: str = None) -> str:
        room_id = str(uuid.uuid4())
        
        self.room_members[room_id] = set(members)
        self.message_history[room_id] = []
        
        for user_id in members:
            if user_id not in self.user_rooms:
                self.user_rooms[user_id] = set()
            self.user_rooms[user_id].add(room_id)
        
        self.room_metadata[room_id] = {
            "type": room_type,
            "name": room_name or f"{room_type.capitalize()} Chat",
            "created_at": datetime.now().isoformat(),
            "members": members
        }
        
        print(f"Created {room_type} room: {room_id} with {len(members)} members")
        return room_id
    
    def get_or_create_private_room(self, user1: str, user2: str) -> str:
        for room_id, metadata in self.room_metadata.items():
            if metadata["type"] == "private":
                members = set(metadata["members"])
                if members == {user1, user2}:
                    return room_id
        
        return self.create_room("private", [user1, user2])
    
    def verify_room_access(self, user_id: str, room_id: str) -> bool:
        if room_id not in self.room_members:
            return False
        return user_id in self.room_members[room_id]
    
    def get_user_login_time(self, user_id: str) -> datetime:
        return self.user_login_times.get(user_id, datetime.now())
    
    async def join_room(self, user_id: str, room_id: str):
        if room_id not in self.room_members:
            return False
        
        self.room_members[room_id].add(user_id)
        if user_id not in self.user_rooms:
            self.user_rooms[user_id] = set()
        self.user_rooms[user_id].add(room_id)
        return True
    
    async def leave_room(self, user_id: str, room_id: str):
        if room_id == "public":
            return False
        
        if room_id in self.room_members:
            self.room_members[room_id].discard(user_id)
        
        if user_id in self.user_rooms:
            self.user_rooms[user_id].discard(room_id)
        
        return True
    
    async def broadcast_to_room(self, room_id: str, message: dict, exclude_from_history: bool = False):
        if not exclude_from_history:
            if room_id not in self.message_history:
                self.message_history[room_id] = []
            
            self.message_history[room_id].append({
                **message,
                "room_id": room_id,
                "stored_at": datetime.now().isoformat()
            })
        
        if room_id not in self.room_members:
            return
        
        disconnected = []
        for user_id in self.room_members[room_id]:
            if user_id in self.active_connections:
                try:
                    message_with_room = {**message, "room_id": room_id}
                    await self.active_connections[user_id].send_json(message_with_room)
                except Exception as e:
                    print(f"Error sending to {user_id}: {e}")
                    disconnected.append(user_id)
        
        for user_id in disconnected:
            self.disconnect(user_id)
    
    async def send_to_user(self, user_id: str, message: dict):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except Exception as e:
                print(f"Error sending to {user_id}: {e}")
                self.disconnect(user_id)
    
    async def broadcast_user_list(self):
        user_list = list(self.active_connections.keys())
        message = {
            "type": "online_users",
            "users": user_list,
            "count": len(user_list),
            "timestamp": datetime.now().isoformat()
        }
        await self.broadcast_to_room("public", message, exclude_from_history=True)
    
    def get_room_history(self, room_id: str, user_id: str, limit: int = 100):
        if not self.verify_room_access(user_id, room_id):
            return []
        
        if room_id not in self.message_history:
            return []
        
        user_login_time = self.get_user_login_time(user_id)
        
        filtered_messages = []
        for msg in self.message_history[room_id]:
            msg_timestamp_str = msg.get("timestamp") or msg.get("stored_at")
            if msg_timestamp_str:
                try:
                    msg_timestamp = datetime.fromisoformat(msg_timestamp_str.replace('Z', '+00:00'))
                    if msg_timestamp.tzinfo:
                        msg_timestamp = msg_timestamp.replace(tzinfo=None)
                    
                    if msg_timestamp >= user_login_time:
                        filtered_messages.append(msg)
                except:
                    filtered_messages.append(msg)
            else:
                filtered_messages.append(msg)
        
        return filtered_messages[-limit:]
    
    def get_user_rooms(self, user_id: str):
        if user_id not in self.user_rooms:
            return []
        
        rooms = []
        for room_id in self.user_rooms[user_id]:
            room_info = {
                "room_id": room_id,
                "member_count": len(self.room_members.get(room_id, set()))
            }
            
            if room_id in self.room_metadata:
                metadata = self.room_metadata[room_id]
                room_info.update({
                    "type": metadata["type"],
                    "name": metadata["name"],
                    "created_at": metadata["created_at"]
                })
                if metadata["type"] == "private":
                    other_user = [m for m in metadata["members"] if m != user_id][0]
                    room_info["other_user"] = other_user
                    room_info["name"] = other_user
            elif room_id == "public":
                room_info.update({
                    "type": "public",
                    "name": "Public Chat"
                })
            
            rooms.append(room_info)
        
        return rooms

manager = ConnectionManager()

@app.get("/")
async def root():
    return {
        "name": "Bid Chat API - Production",
        "version": "4.0.0",
        "status": "running",
        "domain": "chat.buildingindiadigital.com",
        "active_users": len(manager.active_connections),
        "total_rooms": len(manager.room_members),
        "security": "Messages only visible to users logged in when sent",
        "features": [
            "public_chat", 
            "private_chat", 
            "group_chat", 
            "file_sharing", 
            "location_sharing",
            "time_based_message_filtering"
        ]
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "active_connections": len(manager.active_connections)
    }

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        file_extension = os.path.splitext(file.filename)[1]
        safe_filename = f"{timestamp}_{unique_id}{file_extension}"
        file_path = UPLOAD_DIR / safe_filename
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        file_type = "file"
        content_type = file.content_type or ""
        
        if content_type.startswith("image/"):
            file_type = "image"
        elif content_type.startswith("video/"):
            file_type = "video"
        elif content_type.startswith("audio/"):
            file_type = "audio"
        elif "pdf" in content_type or "document" in content_type or content_type.startswith("application/"):
            file_type = "document"
        
        file_size = os.path.getsize(file_path)
        file_url = f"https://chat.buildingindiadigital.com/uploads/{safe_filename}"
        
        print(f"File uploaded: {safe_filename} ({file_size} bytes)")
        
        return {
            "success": True,
            "filename": file.filename,
            "saved_filename": safe_filename,
            "file_url": file_url,
            "file_type": file_type,
            "file_size": file_size,
            "content_type": content_type
        }
    
    except Exception as e:
        print(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@app.get("/uploads/{filename}")
async def get_file(filename: str):
    file_path = UPLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@app.post("/rooms/private")
async def create_private_chat(user1: str, user2: str):
    if not user1 or not user2:
        raise HTTPException(status_code=400, detail="Both users required")
    
    if user1 == user2:
        raise HTTPException(status_code=400, detail="Cannot create private chat with yourself")
    
    room_id = manager.get_or_create_private_room(user1, user2)
    room_info = manager.room_metadata.get(room_id, {})
    
    return {
        "success": True,
        "room_id": room_id,
        "type": "private",
        "members": [user1, user2],
        **room_info
    }

@app.post("/rooms/group")
async def create_group_chat(members: List[str], name: str = None):
    if len(members) < 2:
        raise HTTPException(status_code=400, detail="At least 2 members required")
    
    room_id = manager.create_room("group", members, name)
    room_info = manager.room_metadata.get(room_id, {})
    
    return {
        "success": True,
        "room_id": room_id,
        "type": "group",
        "members": members,
        **room_info
    }

@app.get("/rooms/{user_id}")
async def get_user_rooms(user_id: str):
    rooms = manager.get_user_rooms(user_id)
    return {
        "success": True,
        "user_id": user_id,
        "rooms": rooms,
        "count": len(rooms)
    }

@app.get("/rooms/{room_id}/history")
async def get_room_history(room_id: str, user_id: str, limit: int = 100):
    if not manager.verify_room_access(user_id, room_id):
        raise HTTPException(status_code=403, detail="Access denied to this room")
    
    messages = manager.get_room_history(room_id, user_id, limit)
    return {
        "success": True,
        "room_id": room_id,
        "messages": messages,
        "count": len(messages),
        "filtered": "Only messages sent after your first login"
    }

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    
    if not user_id or len(user_id.strip()) == 0:
        await websocket.close(code=1008, reason="Invalid user_id")
        return
    
    user_id = user_id.strip()
    await manager.connect(user_id, websocket)
    
    try:
        await manager.send_to_user(user_id, {
            "type": "connection",
            "status": "connected",
            "message": f"Welcome {user_id}!",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "login_time": manager.get_user_login_time(user_id).isoformat()
        })
        
        user_rooms = manager.get_user_rooms(user_id)
        await manager.send_to_user(user_id, {
            "type": "rooms_list",
            "rooms": user_rooms,
            "count": len(user_rooms)
        })
        
        public_history = manager.get_room_history("public", user_id, limit=100)
        if public_history:
            await manager.send_to_user(user_id, {
                "type": "chat_history",
                "room_id": "public",
                "messages": public_history,
                "count": len(public_history)
            })
        
        for room_id in manager.user_rooms.get(user_id, set()):
            if room_id != "public":
                room_history = manager.get_room_history(room_id, user_id, limit=100)
                if room_history:
                    await manager.send_to_user(user_id, {
                        "type": "chat_history",
                        "room_id": room_id,
                        "messages": room_history,
                        "count": len(room_history)
                    })
        
        await manager.broadcast_user_list()
        
        await manager.broadcast_to_room("public", {
            "type": "user_joined",
            "user_id": user_id,
            "message": f"{user_id} joined the chat",
            "timestamp": datetime.now().isoformat()
        }, exclude_from_history=True)
        
        while True:
            data = await websocket.receive_text()
            
            try:
                message_data = json.loads(data)
            except json.JSONDecodeError:
                print(f"Invalid JSON from {user_id}")
                continue
            
            room_id = message_data.get("room_id", "public")
            
            if not manager.verify_room_access(user_id, room_id):
                await manager.send_to_user(user_id, {
                    "type": "error",
                    "message": "Access denied to this room",
                    "room_id": room_id
                })
                continue
            
            message_type = message_data.get("message_type", "text")
            
            message = {
                "type": "message",
                "message_type": message_type,
                "sender_id": user_id,
                "room_id": room_id,
                "timestamp": datetime.now().isoformat()
            }
            
            if message_type == "text":
                content = message_data.get("content", "").strip()
                if content:
                    message["content"] = content
                    await manager.broadcast_to_room(room_id, message)
            
            elif message_type == "location":
                latitude = message_data.get("latitude")
                longitude = message_data.get("longitude")
                address = message_data.get("address", "Location")
                
                if latitude is not None and longitude is not None:
                    message["latitude"] = float(latitude)
                    message["longitude"] = float(longitude)
                    message["address"] = address
                    await manager.broadcast_to_room(room_id, message)
            
            elif message_type in ["image", "video", "audio", "document", "file"]:
                file_url = message_data.get("file_url", "")
                if file_url:
                    message["file_url"] = file_url
                    message["filename"] = message_data.get("filename", "file")
                    message["file_size"] = message_data.get("file_size", 0)
                    message["caption"] = message_data.get("caption", "")
                    await manager.broadcast_to_room(room_id, message)
    
    except WebSocketDisconnect:
        print(f"User {user_id} disconnected normally")
    except Exception as e:
        print(f"Error in websocket for {user_id}: {e}")
    finally:
        manager.disconnect(user_id)
        await manager.broadcast_user_list()
        
        await manager.broadcast_to_room("public", {
            "type": "user_left",
            "user_id": user_id,
            "message": f"{user_id} left the chat",
            "timestamp": datetime.now().isoformat()
        }, exclude_from_history=True)

@app.get("/users/online")
async def get_online_users():
    return {
        "success": True,
        "online_users": list(manager.active_connections.keys()),
        "count": len(manager.active_connections)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
