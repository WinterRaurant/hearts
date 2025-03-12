from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import random
import string
import asyncio
import time

app = FastAPI()

# 允许跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源，生产环境请限制特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 服务器中的游戏房间信息
rooms = {}
players = {}  # 存储玩家信息，包括手牌和 WebSocket 连接

class Player:
    def __init__(self, player_id, websocket: WebSocket = None):
        self.player_id = player_id
        self.websocket = websocket
        self.hand = []

    async def send_message(self, message):
        if self.websocket:  # 确保 WebSocket 连接存在
            await self.websocket.send_json(message)

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = []
        self.deck = []
        self.hands = {}
        self.current_turn = 0
        self.scores = {}
        self.trick = []  # 当前回合的牌
        self.trick_suit = None  # 当前回合的花色
        self.game_started = False
        self.hearts_broken = False  # 是否已经有人出过♥️

    def validate_play(self, player_id, card):
        """ 检查玩家的出牌是否合法 """

        if player_id != self.players[self.current_turn]:
            return {"error": "Not your turn"}

        if card not in self.hands[player_id]:
            return {"error": "Invalid card"}

        # **第一轮第一张牌必须是 ♣️2**
        if len(self.trick) == 0 and not self.hearts_broken and sum(len(hand) for hand in self.hands.values()) == 52:
            if card["rank"] != 2 or card["suit"] != "♣️":
                return {"error": "The first move must be the 2 of ♣️"}
        
        # 如果是本轮的第一张牌
        if len(self.trick) == 0:
            # 如果♥️未被破坏，不能率先出♥️，除非玩家只有♥️
            if card["suit"] == "❤️" and not self.hearts_broken:
                if any(c["suit"] != "❤️" for c in self.hands[player_id]):
                    return {"error": "You cannot lead with hearts until it is broken"}

            self.trick_suit = card["suit"]

        else:
            # 必须跟随当前的花色
            if card["suit"] != self.trick_suit:
                if any(c["suit"] == self.trick_suit for c in self.hands[player_id]):
                    return {"error": f"You must play a {self.trick_suit} card if you have one"}

        return None  # 通过规则检查
    
    def generate_deck(self):
        # suits = ['hearts', 'diamonds', 'clubs', 'spades']
        suits = ['❤️', '♦️', '♣️', '♠️']
        ranks = list(range(2, 15))  # 2-10, 11(J), 12(Q), 13(K), 14(A)
        deck = [{'suit': suit, 'rank': rank} for suit in suits for rank in ranks]
        random.shuffle(deck)
        return deck
    
    async def deal_cards(self):
        """ 仅在游戏开始时调用一次，发 13 张牌 """
        self.deck = self.generate_deck()
        num_players = len(self.players)
        for i, player in enumerate(self.players):
            self.hands[player] = sorted(
                self.deck[i::num_players],
                key=lambda card: ({"♠️": 0, "❤️": 1, "♣️": 2, "♦️": 3}[card["suit"]], card["rank"])
            )

        # 设定第一轮的先手玩家（持有♣️2）
        for player in self.players:
            if any(card["rank"] == 2 and card["suit"] == "♣️" for card in self.hands[player]):
                self.current_turn = self.players.index(player)
                break

        self.trick = []
        self.trick_suit = None
        self.hearts_broken = False
        self.game_started = True

        # 发送手牌给所有玩家
        for player in self.players:
            await players[player].send_message({
                "message": f"Game started",
                "hand": self.hands[player],
                "first_player": self.players[self.current_turn],
                "scores": self.scores
            })


    def get_next_player(self):
        self.current_turn = (self.current_turn + 1) % 4
        return self.players[self.current_turn]
    
    def play_card(self, player_id, card):
        error = self.validate_play(player_id, card)
        if error:
            return error  # 返回错误信息

        self.hands[player_id].remove(card)

        self.trick.append((player_id, card))

        # 记录♥️是否被打出
        if card["suit"] == "❤️":
            self.hearts_broken = True

        # 本轮结束，处理赢家
        if len(self.trick) == 4:
            self.resolve_trick()
        else:
            self.current_turn = (self.current_turn + 1) % 4  # 轮到下一个玩家

        return {
            "message": "Card played",
            "player": player_id,
            "card": card,
            "next_player": self.players[self.current_turn],
            "scores": self.scores
        }
    
    def resolve_trick(self):
        """ 计算本轮胜者，设置下一轮先手玩家，并计算分数 """
        highest_card = max(
            (c for c in self.trick if c[1]["suit"] == self.trick_suit),
            key=lambda x: x[1]["rank"]
        )
        winner = highest_card[0]
        self.current_turn = self.players.index(winner)  # 设置下一轮先手玩家

        # 计算得分
        points = sum(1 for _, card in self.trick if card["suit"] == "❤️")
        points += 13 if any(card["suit"] == "♠️" and card["rank"] == 12 for _, card in self.trick) else 0
        self.scores[winner] += points

        self.trick = []
        self.trick_suit = None

        # **检测游戏是否结束**
        if all(len(self.hands[player]) == 0 for player in self.players):
            asyncio.create_task(self.end_round())

    async def end_round(self):
        score_changes = {p: self.scores[p] for p in self.players}
        for player in self.players:
            await players[player].send_message({
                "message": "Round ended",
                "score_changes": score_changes,
                "total_scores": self.scores
            })
        if max(self.scores.values()) >= 50:
            await self.reset_game()
        else:
            await self.deal_cards()

    async def reset_game(self):
        for player in self.players:
            await players[player].send_message({
                "message": "Game over, scores reset",
                "final_scores": self.scores
            })
        self.scores = {p: 0 for p in self.players}
        await self.deal_cards()

    def end_game(self):
        """ 游戏结束，计算最终得分并广播 """
        result = sorted(self.scores.items(), key=lambda x: x[1])  # 按得分排序
        winner = result[0][0]  # 得分最低者获胜

        # 向所有玩家发送最终得分
        for player in self.players:
            asyncio.create_task(players[player].send_message({
                "message": "Game over",
                "scores": self.scores,
                "winner": winner
            }))

        # 移除房间
        del rooms[self.room_id]

@app.post("/create_room")
async def create_room():
    room_id = ''.join(random.choices(string.digits, k=4))
    while room_id in rooms:
        room_id = ''.join(random.choices(string.digits, k=4))
    rooms[room_id] = GameRoom(room_id)
    return {"room_id": room_id}

@app.post("/join_room/{room_id}")
async def join_room(room_id: str, player_id: str):
    if room_id not in rooms:
        return {"error": "Room not found"}
    room = rooms[room_id]
    if len(room.players) >= 4:
        return {"error": "Room is full"}
    if player_id in room.players:
        return {"error": "Player already in room"}
    
    room.players.append(player_id)

    # 仅在第一次加入时初始化得分，防止重复设置为0
    if player_id not in room.scores:
        room.scores[player_id] = 0  

    if player_id not in players:
        players[player_id] = Player(player_id, None)

    return {"message": "Joined room", "players": room.players}

    # if len(room.players) == 4:
    #     print('now deal!')
    #     await room.deal_cards()
    

@app.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, player_id: str):
    await websocket.accept()
    
    if room_id not in rooms:
        await websocket.close()
        return
    
    room = rooms[room_id]
    
    if player_id not in room.players:
        await websocket.close()
        return

    # 绑定 WebSocket 连接
    players[player_id].websocket = websocket

    # 检查是否所有玩家都已连接
    if len(room.players) == 4 and all(players[p].websocket is not None for p in room.players):
        await room.deal_cards()
        # # 发送手牌给所有玩家
        # for p in room.players:
        #     await players[p].send_message({"message": "Game started", "hand": hands[p]})

    try:
        while True:
            data = await websocket.receive_json()
            if data["action"] == "play_card":
                response = room.play_card(player_id, data["card"])
                if "error" not in response:
                    for p in room.players:
                        await players[p].send_message(response)
                else:
                    await players[player_id].send_message(response)
    except WebSocketDisconnect:
        room.players.remove(player_id)
        del players[player_id]
        if not room.players:
            del rooms[room_id]
        await websocket.close()
