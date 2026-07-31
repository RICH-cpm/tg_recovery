from fastapi.templating import Jinja2Templates
from .config import config
templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
