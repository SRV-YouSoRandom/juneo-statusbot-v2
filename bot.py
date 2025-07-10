from logging import exception
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
import json
import os
import time
import asyncio
import aiohttp
from dotenv import load_dotenv
from collections import deque

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID")) if os.getenv("REPORT_CHANNEL_ID") else None
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None

# API Endpoints
rpc_url_pchain = "https://api.juneo-mainnet.network/ext/bc/P"
rpc_url_june_chain = "https://rpc.juneo-mainnet.network/ext/bc/JUNE/rpc"
subscribers_file = "subscribers.json"
notifications_file = "notifications.json"

# Rate limiting configuration
class RateLimiter:
    def __init__(self, max_messages_per_minute=30, max_burst=5):
        self.max_messages_per_minute = max_messages_per_minute
        self.max_burst = max_burst
        self.message_queue = deque()
        self.last_message_times = deque()
        self.is_processing = False
    
    async def can_send_message(self):
        """Check if we can send a message without hitting rate limits"""
        now = time.time()
        
        # Remove timestamps older than 1 minute
        while self.last_message_times and now - self.last_message_times[0] > 60:
            self.last_message_times.popleft()
        
        # Check if we're under the per-minute limit
        if len(self.last_message_times) >= self.max_messages_per_minute:
            return False
        
        # Check if we're under the burst limit (messages in last 5 seconds)
        recent_messages = sum(1 for t in self.last_message_times if now - t < 5)
        if recent_messages >= self.max_burst:
            return False
        
        return True
    
    async def add_message_to_queue(self, user_id, node_id, node_name, validator_info):
        """Add a message to the queue for rate-limited sending"""
        message_data = {
            'user_id': user_id,
            'node_id': node_id,
            'node_name': node_name,
            'validator_info': validator_info,
            'timestamp': time.time()
        }
        self.message_queue.append(message_data)
        
        # Start processing if not already running
        if not self.is_processing:
            asyncio.create_task(self.process_message_queue())
    
    async def process_message_queue(self):
        """Process the message queue with rate limiting"""
        self.is_processing = True
        
        while self.message_queue:
            if await self.can_send_message():
                message_data = self.message_queue.popleft()
                
                # Check if message is still relevant (not too old)
                if time.time() - message_data['timestamp'] < 300:  # 5 minutes
                    success = await self._send_notification(
                        message_data['user_id'],
                        message_data['node_id'],
                        message_data['node_name'],
                        message_data['validator_info']
                    )
                    
                    if success:
                        self.last_message_times.append(time.time())
                        # Small delay between messages to be extra safe
                        await asyncio.sleep(2)
                else:
                    print(f"Discarding old notification for user {message_data['user_id']}, node {message_data['node_id']}")
            else:
                # Wait before checking again
                await asyncio.sleep(10)
        
        self.is_processing = False
    
    async def _send_notification(self, user_id, node_id, node_name, validator_info):
        """Internal method to send the actual notification"""
        try:
            user = bot.get_user(int(user_id))
            if user is None:
                user = await bot.fetch_user(int(user_id))
            
            embed = discord.Embed(
                title="⚠️ Node Alert",
                description=f"Your subscribed node **{node_name}** is not connected!",
                color=discord.Color.red()
            ).add_field(
                name="NodeID",
                value=node_id,
                inline=False
            ).add_field(
                name="Current Uptime",
                value=f"{validator_info['uptime']}%",
                inline=False
            ).add_field(
                name="Status",
                value="🔴 Disconnected",
                inline=False
            ).add_field(
                name="Next Notification",
                value="You will receive another notification in 24 hours if the issue persists. \nIf you wish to stop these alerts please use `/unsub` to unsubscribe from your NodeID!!",
                inline=False
            )
            
            # Try to send DM
            try:
                await user.send(embed=embed)
                print(f"Sent DM notification to user {user_id} for node {node_id}")
                record_notification(user_id, node_id)
                return True
            except discord.Forbidden:
                print(f"Cannot send DM to user {user_id}, trying channel ping")
                await ping_in_channel(user_id, node_id, node_name, validator_info)
                return True
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    print(f"Rate limited when sending DM to user {user_id}")
                    # Re-add to queue for later processing
                    await self.add_message_to_queue(user_id, node_id, node_name, validator_info)
                    return False
                else:
                    print(f"HTTP error sending DM to user {user_id}: {e}")
                    return False
        except Exception as e:
            print(f"Error notifying user {user_id}: {e}")
            return False

# Initialize rate limiter
rate_limiter = RateLimiter(max_messages_per_minute=25, max_burst=3)

# Initialize the bot
intents = discord.Intents.default()
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='.', intents=intents)

# Load or initialize subscribers data
if not os.path.exists(subscribers_file):
    with open(subscribers_file, 'w') as f:
        json.dump({}, f)

# Load or initialize notifications tracking
if not os.path.exists(notifications_file):
    with open(notifications_file, 'w') as f:
        json.dump({}, f)

def load_subscribers():
    with open(subscribers_file, 'r') as f:
        return json.load(f)

def save_subscribers(data):
    with open(subscribers_file, 'w') as f:
        json.dump(data, f)

def load_notifications():
    with open(notifications_file, 'r') as f:
        return json.load(f)

def save_notifications(data):
    with open(notifications_file, 'w') as f:
        json.dump(data, f)

def can_send_notification(user_id, node_id):
    """Check if we can send a notification (only once per day per user per node)"""
    notifications = load_notifications()
    key = f"{user_id}_{node_id}"
    
    if key not in notifications:
        return True
    
    last_notification = datetime.fromisoformat(notifications[key])
    now = datetime.now()
    
    # Check if 24 hours have passed
    return now - last_notification >= timedelta(hours=24)

def record_notification(user_id, node_id):
    """Record that we sent a notification"""
    notifications = load_notifications()
    key = f"{user_id}_{node_id}"
    notifications[key] = datetime.now().isoformat()
    save_notifications(notifications)

def cleanup_old_notifications():
    """Clean up notification records older than 7 days"""
    notifications = load_notifications()
    now = datetime.now()
    cutoff = now - timedelta(days=7)
    
    keys_to_remove = []
    for key, timestamp_str in notifications.items():
        timestamp = datetime.fromisoformat(timestamp_str)
        if timestamp < cutoff:
            keys_to_remove.append(key)
    
    for key in keys_to_remove:
        del notifications[key]
    
    if keys_to_remove:
        save_notifications(notifications)
        print(f"Cleaned up {len(keys_to_remove)} old notification records")

async def fetch_validator_info(node_id):
    response = requests.post(rpc_url_pchain, json={
        "jsonrpc": "2.0",
        "method": "platform.getCurrentValidators",
        "params": {"nodeIDs": [node_id]},
        "id": 1
    })

    if response.status_code == 200:
        result = response.json().get("result", {})
        validators = result.get("validators", [])
        if validators:
            return validators[0]
    return None

async def fetch_multiple_validators(node_ids):
    """Fetch validator info for multiple nodes at once"""
    if not node_ids:
        return []
    
    payload = {
        "jsonrpc": "2.0",
        "method": "platform.getCurrentValidators",
        "params": {"nodeIDs": node_ids},
        "id": 1
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(rpc_url_pchain, json=payload) as resp:
                if resp.status != 200:
                    print(f"API request failed with status {resp.status}")
                    return []
                data = await resp.json()
                return data.get("result", {}).get("validators", [])
        except Exception as e:
            print(f"Error fetching validators: {e}")
            return []

async def fetch_peer_info(node_id):
    response = requests.post("https://rpc.juneo-mainnet.network/ext/info", json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "info.peers",
        "params": {"nodeIDs": [node_id]}
    })

    if response.status_code == 200:
        result = response.json().get("result", {})
        peers = result.get("peers", [])
        if peers:
            return peers[0]
    return None

def format_peer_info(peer_info, node_id):
    # Determine color based on status
    status_color = discord.Color.green() if peer_info and peer_info.get("nodeID") else discord.Color.red()

    # Create the embed
    embed = discord.Embed(
        title="Peer Node Status",
        color=status_color
    )

    # Add fields
    if peer_info:
        status = "Connected" if peer_info.get("nodeID") else "Disconnected"
        observed_uptime = peer_info.get("observedUptime", "0") + "%"
        embed.add_field(
            name="**Status:**",
            value=status,
            inline=False
        ).add_field(
            name="**Peer Uptime:**",
            value=observed_uptime,
            inline=False
        )
    else:
        embed.add_field(
            name="**Status:**",
            value="Disconnected",
            inline=False
        ).add_field(
            name="**Observed Uptime:**",
            value="0%",
            inline=False
        )

    # Add additional info
    embed.add_field(
        name="**NodeID:**",
        value=node_id,
        inline=False
    ).add_field(
        name="**Note:**",
        value="1. If you are a validator then for better status use `/sub` to subscribe to your node\n2. Use `/help` for help if needed.",
        inline=False
    )

    # Add footer
    embed.set_footer(text="Juneo Network", icon_url="https://example.com/juneo_icon.png")

    return embed

async def fetch_block_height_pchain():
    response = requests.post(rpc_url_pchain, json={
        "jsonrpc": "2.0",
        "method": "platform.getHeight",
        "params": {},
        "id": 1
    })

    if response.status_code == 200:
        result = response.json().get("result", {})
        return result.get("height", "N/A")
    return "N/A"

async def fetch_block_height_june_chain():
    response = requests.post(rpc_url_june_chain, json={
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1
    })

    if response.status_code == 200:
        result = response.json().get("result", "0")
        return int(result, 16)  # Convert from hex to decimal
    return "N/A"

def get_uptime_bar(uptime_percentage):
    total_blocks = 10
    filled_blocks = int(uptime_percentage // 10)  # Number of fully filled blocks
    partial_block_percentage = uptime_percentage % 10  # Percentage of the last block
    bar = []

    for i in range(total_blocks):
        if i < filled_blocks:
            bar.append("🟩")  # Full green block
        elif i == filled_blocks:
            # Determine color of the partially filled block
            if partial_block_percentage >= 5:
                bar.append("🟨")  # Partial block, yellow if ≥ 5%
            else:
                bar.append("🟥")  # Partial block, red if < 5%
        else:
            bar.append("🟥")  # Empty red block

    return ''.join(bar)

async def format_validator_info(validator_info):
    if validator_info:
        start_time = datetime.fromtimestamp(int(validator_info['startTime']))
        end_time = datetime.fromtimestamp(int(validator_info['endTime']))

        formatted_start_time = discord.utils.format_dt(start_time, style='F')
        formatted_end_time = discord.utils.format_dt(end_time, style='F')

        status_color = discord.Color.green() if validator_info['connected'] else discord.Color.red()
        uptime_percentage = float(validator_info['uptime'])
        rounded_uptime = round(uptime_percentage)
        uptime_color = (
            discord.Color.green() if rounded_uptime > 97 else
            discord.Color.orange() if rounded_uptime > 91 else
            discord.Color.red()
        )

        status_indicator = ":green_circle:" if validator_info['connected'] else ":red_circle:"

        uptime_bar = get_uptime_bar(uptime_percentage)

        embed = discord.Embed(
            title="Validator Node Status",
            color=status_color
        ).add_field(
            name="**Status:**", 
            value=f"{status_indicator} {'Connected' if validator_info['connected'] else 'Disconnected'}",
            inline=False
        ).add_field(
            name="**Uptime:**", 
            value=f"{uptime_bar} ({uptime_percentage:.2f}%)",
            inline=False
        ).add_field(
            name="**Stake Amount:**", 
            value=f"{int(validator_info['stakeAmount']) / 1e9} JUNE", 
            inline=False
        ).add_field(
            name="**Start Time:**", 
            value=formatted_start_time, 
            inline=False
        ).add_field(
            name="**End Time:**", 
            value=formatted_end_time, 
            inline=False
        ).add_field(
            name="**Delegation Fee:**", 
            value=f"{validator_info['delegationFee']}%", 
            inline=False
        ).add_field(
            name="**Potential Reward:**", 
            value=f"{int(validator_info['potentialReward']) / 1e9} JUNE", 
            inline=False
        ).add_field(
            name="**Delegator Count:**", 
            value=validator_info['delegatorCount'], 
            inline=False
        ).add_field(
            name="**Delegator Weight:**", 
            value=f"{int(validator_info['delegatorWeight']) / 1e9} JUNE", 
            inline=False
        )

        return embed
    else:
        return discord.Embed(
            title="Error", 
            description="No information found for the specified NodeID", 
            color=discord.Color.red()
        )

async def notify_user(user_id, node_id, node_name, validator_info):
    """Queue notification for rate-limited sending"""
    await rate_limiter.add_message_to_queue(user_id, node_id, node_name, validator_info)

async def ping_in_channel(user_id, node_id, node_name, validator_info):
    """Ping user in the report channel if DM fails"""
    if not REPORT_CHANNEL_ID:
        print("No report channel configured")
        return
    
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel is None:
            print(f"Report channel {REPORT_CHANNEL_ID} not found")
            return
        
        embed = discord.Embed(
            title="⚠️ Node Alert",
            description=f"Node is not connected!",
            color=discord.Color.red()
        ).add_field(
            name="NodeID",
            value=node_id,
            inline=False
        ).add_field(
            name="Current Uptime",
            value=f"{validator_info['uptime']}%",
            inline=False
        ).add_field(
            name="Status",
            value="🔴 Disconnected",
            inline=False
        ).add_field(
            name="Note",
            value="This notification is sent because DM delivery failed.\nIf you wish to stop these alerts please use `/unsub` to unsubscribe from your NodeID!!",
            inline=False
        )
        
        await channel.send(f"<@{user_id}> Your node needs attention!", embed=embed)
        print(f"Sent channel ping to user {user_id} for node {node_id}")
        record_notification(user_id, node_id)
    except Exception as e:
        print(f"Error pinging in channel for user {user_id}: {e}")

@tasks.loop(minutes=10)  # Changed to 10mins until more urls are added
async def monitor_subscribed_nodes():
    """Monitor all subscribed nodes and notify users of issues"""
    try:
        print("Starting monitoring check...")
        subscribers = load_subscribers()
        if not subscribers:
            print("No subscribers found")
            return
        
        # Clean up old notification records
        cleanup_old_notifications()
        
        # Collect all unique node IDs
        all_node_ids = set()
        user_nodes = {}  # user_id -> [(node_id, node_name), ...]
        
        for user_id, user_subs in subscribers.items():
            user_nodes[user_id] = []
            for sub in user_subs:
                node_id = sub['node_id']
                node_name = sub['name']
                all_node_ids.add(node_id)
                user_nodes[user_id].append((node_id, node_name))
        
        if not all_node_ids:
            print("No node IDs to monitor")
            return
        
        print(f"Monitoring {len(all_node_ids)} unique nodes for {len(user_nodes)} users")
        
        # Fetch validator info for all nodes at once
        validators = await fetch_multiple_validators(list(all_node_ids))
        current_time = int(time.time())
        
        # Create lookup for validator info
        validator_lookup = {v['nodeID']: v for v in validators}
        
        notifications_queued = 0
        
        # Check each user's nodes
        for user_id, nodes in user_nodes.items():
            for node_id, node_name in nodes:
                validator_info = validator_lookup.get(node_id)
                
                if not validator_info:
                    print(f"No validator info found for node {node_id}")
                    continue
                
                # Check if node is in validation period
                start_time = int(validator_info['startTime'])
                end_time = int(validator_info['endTime'])
                in_validation_period = start_time <= current_time <= end_time
                
                # Only notify if node is not connected AND is in validation period AND we haven't notified recently
                if (not validator_info['connected'] and 
                    in_validation_period and 
                    can_send_notification(user_id, node_id)):
                    
                    # Queue notification for rate-limited sending
                    await notify_user(user_id, node_id, node_name, validator_info)
                    notifications_queued += 1
        
        print(f"Completed monitoring check. Queued {notifications_queued} notifications")
    
    except Exception as e:
        print(f"Error during monitoring: {e}")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        try:
            synced_commands = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced_commands)} commands to guild {GUILD_ID}")
        except Exception as e:
            print(f"Error syncing commands to guild: {e}")
    else:
        try:
            synced_commands = await bot.tree.sync()
            print(f"Synced {len(synced_commands)} commands globally")
        except Exception as e:
            print(f"Error syncing commands globally: {e}")
    
    # Start monitoring task
    if not monitor_subscribed_nodes.is_running():
        monitor_subscribed_nodes.start()
        print("Started node monitoring task (checks every 5 minutes)")

@bot.tree.command(name='check', description='Check the status of a peer node')
@app_commands.describe(node_id='The NodeID of the peer')
async def check(interaction: discord.Interaction, node_id: str):
    await interaction.response.defer()
    
    try:
        peer_info = await fetch_peer_info(node_id)
        embed = format_peer_info(peer_info, node_id)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"Error checking node status: {e}", ephemeral=True)

@bot.tree.command(name='sub', description='Subscribe to a NodeID for monitoring')
@app_commands.describe(node_id='The NodeID to subscribe to', name='Custom name for the NodeID')
async def sub(interaction: discord.Interaction, node_id: str, name: str = None):
    await interaction.response.defer(ephemeral=True)
    
    try:
        subscribers = load_subscribers()
        user_id = str(interaction.user.id)

        if user_id not in subscribers:
            subscribers[user_id] = []

        # Check for existing subscriptions
        if any(sub['node_id'] == node_id for sub in subscribers[user_id]):
            await interaction.followup.send(f"You are already subscribed to NodeID `{node_id}`", ephemeral=True)
            return

        # Check if it's a valid validator
        validator_info = await fetch_validator_info(node_id)
        if not validator_info:
            await interaction.followup.send(f"NodeID `{node_id}` is not a valid validator or is not currently active.", ephemeral=True)
            return

        subscribers[user_id].append({'node_id': node_id, 'name': name or node_id})
        save_subscribers(subscribers)
        
        embed = discord.Embed(
            title="✅ Subscription Successful",
            description=f"Subscribed to NodeID `{node_id}` with name `{name or node_id}`",
            color=discord.Color.green()
        ).add_field(
            name="Monitoring Details",
            value="• Bot checks every 5 minutes\n• Notifications sent once per day maximum\n• Only notified during validation periods\n• DM preferred, channel ping as backup",
            inline=False
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error subscribing to node: {e}", ephemeral=True)

@bot.tree.command(name='unsub', description='Unsubscribe from a NodeID')
async def unsub(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        subscribers = load_subscribers()
        user_subs = subscribers.get(str(interaction.user.id), [])
        if not user_subs:
            await interaction.followup.send("You are not subscribed to any NodeID", ephemeral=True)
            return

        options = [
            discord.SelectOption(label=sub['name'][:100], value=sub['node_id'], description=sub['node_id'][:100])
            for sub in user_subs
        ]

        class UnsubSelect(discord.ui.Select):
            def __init__(self):
                super().__init__(placeholder='Choose a NodeID to unsubscribe from', options=options)

            async def callback(self, select_interaction: discord.Interaction):
                selected_node_id = self.values[0]
                subscribers[str(interaction.user.id)] = [
                    sub for sub in user_subs if sub['node_id'] != selected_node_id
                ]
                save_subscribers(subscribers)
                await select_interaction.response.send_message(f"✅ Unsubscribed from NodeID `{selected_node_id}`", ephemeral=True)

        view = discord.ui.View()
        view.add_item(UnsubSelect())
        await interaction.followup.send("Select a NodeID to unsubscribe from:", view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error processing unsubscription: {e}", ephemeral=True)

@bot.tree.command(name='status', description='Get status of a subscribed NodeID')
async def status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        subscribers = load_subscribers()
        user_subs = subscribers.get(str(interaction.user.id), [])
        if not user_subs:
            await interaction.followup.send("You are not subscribed to any NodeID", ephemeral=True)
            return

        options = [
            discord.SelectOption(label=sub['name'][:100], value=sub['node_id'], description=sub['node_id'][:100])
            for sub in user_subs
        ]

        class StatusSelect(discord.ui.Select):
            def __init__(self):
                super().__init__(placeholder='Choose a NodeID to check status', options=options)

            async def callback(self, select_interaction: discord.Interaction):
                await select_interaction.response.defer(ephemeral=True)
                selected_node_id = self.values[0]
                validator_info = await fetch_validator_info(selected_node_id)
                embed = await format_validator_info(validator_info)
                await select_interaction.followup.send(embed=embed, ephemeral=True)

        view = discord.ui.View()
        view.add_item(StatusSelect())
        await interaction.followup.send("Select a NodeID to get status:", view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error checking status: {e}", ephemeral=True)

@bot.tree.command(name='height', description='Get block height of P-chain and JUNE-chain')
async def height(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        pchain_height = await fetch_block_height_pchain()
        june_chain_height = await fetch_block_height_june_chain()

        embed = discord.Embed(
            title="Block Heights",
            color=discord.Color.blue()
        ).add_field(
            name="P-chain Block Height", 
            value=pchain_height,
            inline=False
        ).add_field(
            name="JUNE-chain Block Height", 
            value=june_chain_height,
            inline=False
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error fetching block heights: {e}", ephemeral=True)

@bot.tree.command(name='help', description='List all available commands')
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Available Commands",
        description="Here are the commands you can use:",
        color=discord.Color.blue()
    ).add_field(
        name="/check <NodeID>", 
        value="Check the status of a validator node (peer info).",
        inline=False
    ).add_field(
        name="/sub <NodeID> [name]", 
        value="Subscribe to a NodeID for monitoring. Get notifications when it goes down during validation periods.",
        inline=False
    ).add_field(
        name="/unsub", 
        value="Unsubscribe from a NodeID.",
        inline=False
    ).add_field(
        name="/status", 
        value="Get detailed status of your subscribed NodeIDs.",
        inline=False
    ).add_field(
        name="/height", 
        value="Get the current block heights of P-chain and JUNE-chain.",
        inline=False
    ).add_field(
        name="/list",
        value="Get a DM with all your subscribed NodeIDs and names.",
        inline=False
    ).add_field(
        name="/help", 
        value="Display this help message.",
        inline=False
    ).add_field(
        name="🔔 Monitoring Info",
        value="• Checks every 5 minutes\n• Max 1 notification per day per node\n• Only during validation periods\n• DM first, channel ping if DM fails",
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='list', description='List all subscribed NodeIDs and Names')
async def list_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        subscribers = load_subscribers()
        user_subs = subscribers.get(str(interaction.user.id), [])

        if not user_subs:
            await interaction.followup.send("You are not subscribed to any NodeID", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Your Subscribed NodeIDs",
            description=f"You have {len(user_subs)} active subscription(s)",
            color=discord.Color.blue()
        )

        for i, sub in enumerate(user_subs, 1):
            embed.add_field(
                name=f"{i}. {sub['name']}", 
                value=f"`{sub['node_id']}`", 
                inline=False
            )

        embed.set_footer(text="Monitoring frequency: Every 5 minutes | Max notifications: 1 per day per node")

        # Send the styled embed as a direct message
        try:
            await interaction.user.send(embed=embed)
            await interaction.followup.send("📬 I have sent you a DM with your subscribed NodeIDs and Names.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to send DM. Make sure your DMs are open.\n\nError: {e}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error retrieving subscriptions: {e}", ephemeral=True)

# Run the bot
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not found in environment variables")
        exit(1)
    
    print("Starting bot...")
    bot.run(TOKEN)