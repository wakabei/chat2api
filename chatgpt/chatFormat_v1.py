import asyncio
import json
import random
import re
import string
import time
import uuid

import pybase64
import websockets
from fastapi import HTTPException

from api.files import get_file_content
from api.models import model_system_fingerprint
from api.tokens import split_tokens_from_content, calculate_image_tokens, num_tokens_from_messages
from utils.Logger import logger

moderation_message = "I'm sorry, I cannot provide or engage in any content related to pornography, violence, or any unethical material. If you have any other questions or need assistance, please feel free to let me know. I'll do my best to provide support and assistance."


async def format_not_stream_response(response, prompt_tokens, max_tokens, model):
    chat_id = f"chatcmpl-{''.join(random.choice(string.ascii_letters + string.digits) for _ in range(29))}"
    system_fingerprint_list = model_system_fingerprint.get(model, None)
    system_fingerprint = random.choice(system_fingerprint_list) if system_fingerprint_list else None
    created_time = int(time.time())
    all_text = ""
    async for chunk in response:
        try:
            if chunk.startswith("data: [DONE]"):
                break
            elif not chunk.startswith("data: "):
                continue
            else:
                chunk = json.loads(chunk[6:])
                if not chunk["choices"][0].get("delta"):
                    continue
                all_text += chunk["choices"][0]["delta"]["content"]
        except Exception as e:
            logger.error(f"Error: {chunk}, error: {str(e)}")
            continue
    content, completion_tokens, finish_reason = await split_tokens_from_content(all_text, max_tokens, model)
    message = {
        "role": "assistant",
        "content": content,
    }
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens
    }
    if not message.get("content"):
        raise HTTPException(status_code=403, detail="No content in the message.")

    data = {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_time,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason
            }
        ],
        "usage": usage
    }
    if system_fingerprint:
        data["system_fingerprint"] = system_fingerprint
    return data


async def head_process_response(response):
    async for chunk in response:
        chunk = chunk.decode("utf-8")
        if chunk.startswith("data: {"):
            chunk_old_data = json.loads(chunk[6:])
            message = chunk_old_data.get("message", {})
            if not message and "error" in chunk_old_data:
                return response, False
            role = message.get('author', {}).get('role')
            if role == 'user' or role == 'system':
                continue

            status = message.get("status")
            if status == "in_progress":
                return response, True
    return response, False


async def stream_response(service, response, model, max_tokens):
    chat_id = f"chatcmpl-{''.join(random.choice(string.ascii_letters + string.digits) for _ in range(29))}"
    created_time = int(time.time())

    chunk_new_data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created_time,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "logprobs": None,
                "finish_reason": None
            }
        ]
    }
    yield f"data: {json.dumps(chunk_new_data)}\n\n"

    async for chunk in response:
        chunk = chunk.decode("utf-8")
        try:
            if chunk.startswith("data: {"):
                chunk_old_data = json.loads(chunk[6:].strip())
        except Exception as e:
            logger.error(f"Error: {chunk}, error: {str(e)}")
            continue


def get_url_from_content(content):
    if isinstance(content, str) and content.startswith('http'):
        try:
            url = re.match(
                r'(?i)\b((?:[a-z][\w-]+:(?:/{1,3}|[a-z0-9%])|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?«»“”‘’]))',
                content.split(' ')[0])[0]
            content = content.replace(url, '').strip()
            return url, content
        except Exception:
            return None, content
    return None, content


def format_messages_with_url(content):
    url_list = []
    while True:
        url, content = get_url_from_content(content)
        if url:
            url_list.append(url)
            logger.info(f"Found a file_url from messages: {url}")
        else:
            break
    if not url_list:
        return content
    new_content = [
        {
            "type": "text",
            "text": content
        }
    ]
    for url in url_list:
        new_content.append({
            "type": "image_url",
            "image_url": {
                "url": url
            }
        })
    return new_content


async def api_messages_to_chat(service, api_messages, upload_by_url=False):
    file_tokens = 0
    chat_messages = []
    for api_message in api_messages:
        role = api_message.get('role')
        content = api_message.get('content')
        if upload_by_url:
            if isinstance(content, str):
                content = format_messages_with_url(content)
        if isinstance(content, list):
            parts = []
            attachments = []
            content_type = "multimodal_text"
            for i in content:
                if i.get("type") == "text":
                    parts.append(i.get("text"))
                elif i.get("type") == "image_url":
                    image_url = i.get("image_url")
                    url = image_url.get("url")
                    detail = image_url.get("detail", "auto")
                    file_content, mime_type = await get_file_content(url)
                    file_meta = await service.upload_file(file_content, mime_type)
                    if file_meta:
                        file_id = file_meta["file_id"]
                        file_size = file_meta["size_bytes"]
                        file_name = file_meta["file_name"]
                        mime_type = file_meta["mime_type"]
                        use_case = file_meta["use_case"]
                        if mime_type.startswith("image/"):
                            width, height = file_meta["width"], file_meta["height"]
                            file_tokens += await calculate_image_tokens(width, height, detail)
                            parts.append({
                                "content_type": "image_asset_pointer",
                                "asset_pointer": f"file-service://{file_id}",
                                "size_bytes": file_size,
                                "width": width,
                                "height": height
                            })
                            attachments.append({
                                "id": file_id,
                                "size": file_size,
                                "name": file_name,
                                "mime_type": mime_type,
                                "width": width,
                                "height": height
                            })
                        else:
                            if not use_case == "ace_upload":
                                await service.check_upload(file_id)
                            file_tokens += file_size // 1000
                            attachments.append({
                                "id": file_id,
                                "size": file_size,
                                "name": file_name,
                                "mime_type": mime_type,
                            })
            metadata = {
                "attachments": attachments
            }
        else:
            content_type = "text"
            parts = [content]
            metadata = {}
        chat_message = {
            "id": f"{uuid.uuid4()}",
            "author": {"role": role},
            "content": {"content_type": content_type, "parts": parts},
            "metadata": metadata
        }
        chat_messages.append(chat_message)
    text_tokens = await num_tokens_from_messages(api_messages, service.resp_model)
    prompt_tokens = text_tokens + file_tokens
    return chat_messages, prompt_tokens
