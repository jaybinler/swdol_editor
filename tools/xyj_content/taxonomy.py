"""Game-backed item types and equipment-slot masks, independent of display names."""

import struct

SLOTS = ((1, 'head', '头部'), (2, 'body', '身体'), (4, 'mainhand', '主手'),
         (8, 'offhand', '副手'), (16, 'feet', '脚部'), (32, 'gloves', '手套'),
         (64, 'throwing', '暗器'), (128, 'neck', '首饰'), (256, 'ring', '手饰'),
         (512, 'accessory', '饰品'), (1024, 'treasure', '法宝'))
ITEM_TYPES = {1: '普通物品', 2: '装备', 4: '套装', 5: '秘笈/符笈'}


def item_categories(record: bytes) -> dict:
    """Keep all native slot bits; unknown bits never silently become a known equipment class."""
    kind = struct.unpack_from('<H', record, 8)[0]
    mask = struct.unpack_from('<I', record, 52)[0] if kind == 2 else 0
    slots = [{'key': key, 'label': label} for bit, key, label in SLOTS if mask & bit]
    categories = [slot['key'] for slot in slots]
    if mask & 12:
        categories.append('weapon')
    if not categories:
        categories.append({1: 'ordinary', 4: 'set', 5: 'book'}.get(kind, 'other'))
    if mask & ~2047:
        categories.append('other')
    return {'is_item': kind in ITEM_TYPES, 'type_label': ITEM_TYPES.get(kind, '非道具记录'),
            'slot_mask': mask, 'slots': slots, 'categories': categories}


def category_options() -> list[dict]:
    """Return stable UI filter keys, including shared slots and non-equipment items."""
    return [{'key': 'all', 'label': '全部道具'}, {'key': 'weapon', 'label': '武器/副手'},
            *({'key': key, 'label': label} for _, key, label in SLOTS if key not in ('mainhand', 'offhand')),
            {'key': 'ordinary', 'label': '普通物品'}, {'key': 'book', 'label': '秘笈/符笈'},
            {'key': 'set', 'label': '套装'}, {'key': 'other', 'label': '其他/未分类'}]
