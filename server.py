from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
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
        self.double = 1
        self.get_scores = []  # 收到的分牌
        self.hand = []
        self.pass_cards = []

    async def send_message(self, message):
        if self.websocket:  # 确保 WebSocket 连接存在
            await self.websocket.send_json(message)

class GameRoom:
    def __init__(self, room_id, rules):
        self.room_id = room_id
        self.players = []
        self.deck = []
        self.hands = {}
        # self.rules = rules
        self.counter = 1 
        self.enable_pass_cards = rules.get('rule5')
        self.enable_moon = rules.get('rule4')
        self.enable_red = rules.get('rule3')
        self.enable_J = rules.get('rule2')
        self.enable_10 = rules.get('rule1')
        self.finish_pass = {}
        self.started = False
        self.current_turn = 0 # 轮到谁了
        self.total_scores = {}
        self.round_scores = {}
        self.trick = []  # 当前回合的牌
        self.trick_suit = None  # 当前回合的花色
        self.game_started = False
        self.hearts_broken = False  # 是否已经有人出过♥️

    async def notice_player_list(self):
        for player in self.players:
            await players[player].send_message({
                "player_change": True,
                "players": self.players
            })

    def remove_player(self, player_id):
        self.players.remove(player_id)
        print(self.total_scores)
        self.total_scores.pop(player_id, 0)
        self.round_scores.pop(player_id, 0)

    def validate_play(self, player_id, card):
        """ 检查玩家的出牌是否合法 """
        if self.counter % 4 != 0 and self.enable_pass_cards and not self.started:
            return {"error": "Wait for others passing"}

        if player_id != self.players[self.current_turn]:
            return {"error": "Not your turn"}

        if card not in self.hands[player_id]:
            return {"error": "Invalid card"}

        # **第一轮第一张牌必须是 ♣️2**
        if len(self.trick) == 0 and not self.hearts_broken and sum(len(hand) for hand in self.hands.values()) == 52:
            if card["rank"] != 2 or card["suit"] != "♣️":
                return {"error": "The first move must be the 2 of ♣️"}
        
         # 第一轮不能出♥️，除非手中只有♥️
        if sum(len(hand) for hand in self.hands.values()) > 48 and card["suit"] == "♥️":
            if any(c["suit"] != "♥️" for c in self.hands[player_id]):
                return {"error": "You cannot play ♥️ on the first round"}

        # 如果是本轮的第一张牌
        if len(self.trick) == 0:
            # 如果♥️未被破坏，不能率先出♥️，除非玩家只有♥️
            if card["suit"] == "♥️" and not self.hearts_broken:
                if any(c["suit"] != "♥️" for c in self.hands[player_id]):
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
        suits = ['♥️', '♦️', '♣️', '♠️']
        ranks = list(range(2, 15))  # 2-10, 11(J), 12(Q), 13(K), 14(A)
        deck = [{'suit': suit, 'rank': rank} for suit in suits for rank in ranks]
        random.shuffle(deck)
        return deck
    
    async def start_game(self):
        self.started = True
        get_cards = {}

        for idx, pid in enumerate(self.players):
            if self.counter % 4 == 1:
                reveiver_idx = (idx + 1) % 4
            elif self.counter % 4 == 2:
                reveiver_idx = (idx + 2) % 4
            elif self.counter % 4 == 3:
                reveiver_idx = (idx + 3) % 4
            reveiver_id = self.players[reveiver_idx]
            player_id = self.players[idx]
            print(f'{player_id} passed {players[player_id].pass_cards} to {reveiver_id}')
            for card in players[player_id].pass_cards:
                self.hands[reveiver_id].append(card)
                print(f'{reveiver_id} get {card} from {player_id}')
            get_cards[reveiver_id] = players[player_id].pass_cards
            players[player_id].pass_cards = []

        for i, player in enumerate(self.players):
            self.hands[player] = sorted(
                self.hands[player],
                key=lambda card: ({"♠️": 0, "♥️": 1, "♣️": 2, "♦️": 3}[card["suit"]], card["rank"])
        )
            
                # 设定第一轮的先手玩家（持有♣️2）
        for player in self.players:
            if any(card["rank"] == 2 and card["suit"] == "♣️" for card in self.hands[player]):
                self.current_turn = self.players.index(player)
                break


        for player in self.players:
            if len(get_cards[player]) > 0:
                await players[player].send_message({
                    "message": f"Game started",
                    "hand": self.hands[player], 
                    "first_player": self.players[self.current_turn],
                    "get_cards": get_cards[player],
                    "scores": self.round_scores,
                    "tot_scores": self.total_scores
                })
    
    async def deal_cards(self):
        """ 仅在游戏开始时调用一次，发 13 张牌 """
        self.deck = self.generate_deck()
        self.started = False

        for i, player in enumerate(self.players):
            self.hands[player] = sorted(
                self.deck[i::4],
                key=lambda card: ({"♠️": 0, "♥️": 1, "♣️": 2, "♦️": 3}[card["suit"]], card["rank"])
            )

        # 开始新一局，本局分数清零
        for player_id in self.players:
            self.round_scores[player_id] = 0  
            players[player_id].double = 1
            players[player_id].get_scores = []

        # 设定第一轮的先手玩家（持有♣️2）
        for player in self.players:
            if any(card["rank"] == 2 and card["suit"] == "♣️" for card in self.hands[player]):
                self.current_turn = self.players.index(player)
                break

        self.finish_pass = {}
        self.trick = []
        self.trick_suit = None
        self.hearts_broken = False
        self.game_started = True

        # 发送手牌给所有玩家
        for player in self.players:
            if self.enable_pass_cards and self.counter % 4 != 0: # 要传牌
                await players[player].send_message({
                    "message": f"Pass cards",
                    "hand": self.hands[player],
                })               
            else:
                await players[player].send_message({
                    "message": f"Game started",
                    "hand": self.hands[player],
                    "first_player": self.players[self.current_turn],
                    "scores": self.round_scores,
                    "tot_scores": self.total_scores
                })


    def get_next_player(self):
        self.current_turn = (self.current_turn + 1) % 4
        return self.players[self.current_turn]
    
    async def pass_card(self, player_id, cards):
        self.finish_pass[player_id] = cards
        for c in cards:
            self.hands[player_id].remove(c)
            players[player_id].pass_cards.append(c)
        await players[player_id].send_message({
            "message": "wait for others",
            "hand": self.hands[player_id]
        })
        return len(self.finish_pass)

    
    def play_card(self, player_id, card):
        error = self.validate_play(player_id, card)
        if error:
            return error  # 返回错误信息

        self.hands[player_id].remove(card)

        self.trick.append((player_id, card))

        # 记录♥️是否被打出
        if card["suit"] == "♥️":
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
            "current_suit": self.trick_suit,
            "scores": self.round_scores
        }
    
    """待整顿重灾区"""
    def resolve_trick(self):
        """ 计算本轮胜者，设置下一轮先手玩家，并计算分数 """
        highest_card = max(
            (c for c in self.trick if c[1]["suit"] == self.trick_suit),
            key=lambda x: x[1]["rank"]
        )
        winner = highest_card[0]
        self.current_turn = self.players.index(winner)  # 设置下一轮先手玩家
        for c in self.trick:
            players[winner].get_scores.append(c[1])

        points = 0
        if self.enable_10:
            if any(card["suit"] == "♣️" and card["rank"] == 10 for _, card in self.trick) or players[winner].double == 2:   
                self.round_scores[winner] *= 2
                players[winner].double = 2 
        if self.enable_J:
            points -= 13 if any(card["suit"] == "♦️" and card["rank"] == 11 for _, card in self.trick) else 0

        # 计算得分
        points += sum(1 for _, card in self.trick if card["suit"] == "♥️")
        points += 13 if any(card["suit"] == "♠️" and card["rank"] == 12 for _, card in self.trick) else 0
        self.round_scores[winner] += points * players[winner].double

        self.trick = []
        self.trick_suit = None

        # **检测游戏是否结束**
        if all(len(self.hands[player]) == 0 for player in self.players):
            asyncio.create_task(self.end_round())

    async def end_round(self):
        self.counter += 1
        moon = None
        red = None
        if self.enable_moon :
            for player in self.players:
                if sum(c['suit'] == "♥️" for c in players[player].get_scores) == 13 and any(c['suit'] == "♠️" and c['rank'] == 12 for c in players[player].get_scores):
                    moon = player
                    break
        if not moon and self.enable_red:
            for player in self.players:
                if sum(c['suit'] == "♥️" for c in players[player].get_scores) == 13:
                    red = player
                    self.round_scores[player] -= 26 * players[player].double
                    break

        if moon:
            for player in self.players:
                if player != moon:
                    self.round_scores[player] += 26 * players[player].double
                else:
                    self.round_scores[player] = -13 * players[player].double if any(c['suit'] == "♦️" and c['rank'] == 11 for c in players[player].get_scores) else 0

        self.total_scores = {p: self.total_scores[p] + self.round_scores[p] for p in self.players}

        if moon:
            msg = f"Round ended, {moon} shot the moon!"
        elif red:
            msg = f"Round ended, {red} took all hearts!"
        else:
            msg = "Round ended."

        for player in self.players:
            await players[player].send_message({
                "message": msg,
                "tot_scores": self.total_scores,
                "scores": self.round_scores
            })
        if max(self.total_scores.values()) >= 50:
            await self.reset_game()
        else:
            await self.deal_cards()

    async def reset_game(self):
        for player in self.players:
            await players[player].send_message({
                "message": "Game over, scores reset",
                "final_scores": self.total_scores
            })
        self.total_scores = {p: 0 for p in self.players}
        self.round_scores = {p: 0 for p in self.players}
        await self.deal_cards()

    def end_game(self):
        """ 游戏结束，计算最终得分并广播 """
        result = sorted(self.total_scores.items(), key=lambda x: x[1])  # 按得分排序
        winner = result[0][0]  # 得分最低者获胜

        # 向所有玩家发送最终得分
        for player in self.players:
            asyncio.create_task(players[player].send_message({
                "message": "Game over",
                "tot_scores": self.total_scores,
                "winner": winner
            }))

        # 移除房间
        del rooms[self.room_id]

def create_game_room(creator_id, rules):
    room_id = ''.join(random.choices(string.digits, k=4))
    while room_id in rooms:
        room_id = ''.join(random.choices(string.digits, k=4))
    rooms[room_id] = GameRoom(room_id, rules)
    return room_id

@app.post("/create_room")
async def create_room(request: Request, player_id: str):
    data = await request.json()
    rules = data.get("rules", {})
    # 创建房间时保存这些规则
    room_id = create_game_room(player_id, rules)
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

    if player_id not in players:
        players[player_id] = Player(player_id, None)

    return {"message": "Joined room", "players": room.players}


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
    await room.notice_player_list()

    # 检查是否所有玩家都已连接
    if len(room.players) == 4 and all(players[p].websocket is not None for p in room.players):
        room.total_scores = {p : 0 for p in room.players}
        await room.deal_cards()

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
            if data["action"] == "pass_cards":
                if await room.pass_card(player_id, data['cards']) == 4:
                    await room.start_game()
    except WebSocketDisconnect:
        print(f'{player_id} closed!!!!')
        # room.players.remove(player_id)
        room.remove_player(player_id)
        del players[player_id]
        if not room.players:
            del rooms[room_id]
        else:
            await room.notice_player_list()
        await websocket.close()