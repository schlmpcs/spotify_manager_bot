from aiogram.fsm.state import State, StatesGroup


class OwnerStates(StatesGroup):
    setting_style = State()      # capturing the manager's writing-style prompt
    editing_draft = State()      # editing a drafted reply before sending
