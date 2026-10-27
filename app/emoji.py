def e(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


SEARCH = e("5231012545799666522", "🔍")
NOTIFY = e("5458603043203327669", "🔔")
SETTINGS = e("5341715473882955310", "⚙️")
CHECK = e("5375338737028841420", "🔄")
OK = e("5206607081334906820", "✔️")
STOP = e("5260293700088511294", "⛔️")
ADD = e("5397916757333654639", "➕")
REMOVE = e("5445267414562389170", "🗑")
LINK = e("5271604874419647061", "🔗")
BROADCAST = e("5980911993140284450", "💬")
