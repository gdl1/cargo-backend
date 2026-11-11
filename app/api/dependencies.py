from fastapi import Request


def get_current_user_id(request: Request) -> str:
    return request.app.state.settings.demo_user_id
