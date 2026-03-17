"""Discord :x: リアクション付きメッセージ自動削除

指定チャンネルのメッセージを走査し、❌ (:x:) リアクションが
付いているメッセージをすべて削除する。
GitHub Actions から1日2回（JST 6:00 / 18:00）実行される想定。
"""

import asyncio
import logging
import os
import sys

import discord

# ── ログ設定 ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── 環境変数 ────────────────────────────────────────
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_CLEANUP_CHANNEL_ID", "")

# ❌ の Unicode 表現
X_EMOJI = "\u274c"


async def cleanup_channel(bot_token: str, channel_id: int) -> dict:
    """指定チャンネルで :x: リアクション付きメッセージを削除する。

    Args:
        bot_token: Discord Bot トークン
        channel_id: 対象チャンネルの ID

    Returns:
        {"scanned": int, "deleted": int} の辞書
    """
    intents = discord.Intents.default()
    intents.message_content = True
    intents.reactions = True

    client = discord.Client(intents=intents)
    result = {"scanned": 0, "deleted": 0}

    @client.event
    async def on_ready():
        logger.info("Bot ログイン完了: %s", client.user)
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)

            if channel is None:
                logger.error("チャンネル ID %s が見つかりません", channel_id)
                await client.close()
                return

            logger.info("対象チャンネル: #%s (%s)", channel.name, channel.id)

            # チャンネル内の全メッセージを走査
            async for message in channel.history(limit=None):
                result["scanned"] += 1

                # リアクションに ❌ が含まれているか確認
                has_x = False
                for reaction in message.reactions:
                    emoji = reaction.emoji
                    # Unicode 絵文字の場合
                    if isinstance(emoji, str) and emoji == X_EMOJI:
                        has_x = True
                        break
                    # カスタム絵文字の場合 (:x: がカスタムで登録されている可能性)
                    if hasattr(emoji, "name") and emoji.name == "x":
                        has_x = True
                        break

                if has_x:
                    try:
                        await message.delete()
                        result["deleted"] += 1
                        logger.info(
                            "削除: [%s] %s",
                            message.author.display_name,
                            message.content[:80] if message.content else "(embed)",
                        )
                    except discord.Forbidden:
                        logger.warning(
                            "権限不足で削除できません: message_id=%s", message.id
                        )
                    except discord.HTTPException as e:
                        logger.warning(
                            "削除失敗 (message_id=%s): %s", message.id, e
                        )

            logger.info(
                "完了: %d 件スキャン、%d 件削除",
                result["scanned"],
                result["deleted"],
            )
        except Exception:
            logger.exception("クリーンアップ中にエラーが発生しました")
        finally:
            await client.close()

    await client.start(bot_token)
    return result


def main():
    """エントリーポイント"""
    if not BOT_TOKEN:
        logger.error("環境変数 DISCORD_BOT_TOKEN が設定されていません")
        sys.exit(1)

    if not CHANNEL_ID:
        logger.error("環境変数 DISCORD_CLEANUP_CHANNEL_ID が設定されていません")
        sys.exit(1)

    try:
        channel_id_int = int(CHANNEL_ID)
    except ValueError:
        logger.error("DISCORD_CLEANUP_CHANNEL_ID が数値ではありません: %s", CHANNEL_ID)
        sys.exit(1)

    logger.info("Discord クリーンアップ開始 (チャンネル ID: %s)", channel_id_int)
    result = asyncio.run(cleanup_channel(BOT_TOKEN, channel_id_int))
    logger.info("結果: %d 件スキャン、%d 件削除", result["scanned"], result["deleted"])


if __name__ == "__main__":
    main()
