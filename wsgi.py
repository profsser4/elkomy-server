"""
نقطة تشغيل السيرفر على الاستضافة.
بترجّع البيانات من القاعدة الأول، وبعدين بتشغّل bot.py زي ما هو من غير أي تعديل عليه.
"""
import os

_data_dir = os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else ".")
os.makedirs(_data_dir, exist_ok=True)

import storage_sync  # noqa: E402

storage_sync.start(_data_dir)

from bot import app  # noqa: E402,F401
