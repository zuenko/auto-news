import json
import os
import traceback
from datetime import datetime

from notion_client import Client
import pytz


class NotionAgent:
    """
    A notion agent to operate page/database
    """
    def __init__(self, api_key):
        self.api_key = api_key

        self.api = self._init_client(self.api_key)
        self.databases = {}  # <source, {database_id}>

    def _init_client(self, api_key):
        return Client(auth=api_key)

    def addDatabase(self, source_name, database_id):
        self.databases[source_name] = {
            "database_id": database_id,
        }

    def _extractRichText(self, data):
        content = ""

        for rich_text in data:
            text = rich_text["plain_text"]
            print(f"Block text: {text}")

            content += text

        return content

    def _extractTableRow(self, table_row):
        cells = table_row["cells"]
        content = ""

        for cell in cells:
            # Like rich_text, one cell may contact with
            # multiple cell pieces
            for cell_data in cell:
                content += cell_data["plain_text"]
                print(f"cell data: {cell_data['plain_text']}")

            content += ","

        return content

    def extractPageBlocks(self, page_id, ignore_embed=True):
        content = ""
        metadata = {}

        childs = self.api.blocks.children.list(block_id=page_id).get("results")
        # print(f"n: {len(childs)}, childs: {childs}")

        # only extrace paragraph (ignore embeded content)
        for block in childs:
            block_id = block["id"]
            metadata[block_id] = {}

            print(f"Read block type: {block['type']}, block: {block}")

            if block["type"] == "paragraph":
                text = self._extractRichText(block["paragraph"]["rich_text"])
                content += text
                metadata[block_id]["text"] = text

            elif block["type"] == "embed":
                if ignore_embed:
                    continue

            elif block["type"] == "bulleted_list_item":
                text = self._extractRichText(block["bulleted_list_item"]["rich_text"])
                content += text
                metadata[block_id]["text"] = text

            elif block["type"] == "heading_2":
                text = self._extractRichText(block["heading_2"]["rich_text"])
                content += text
                metadata[block_id]["text"] = text

            elif block["type"] == "table":
                # depth forward in the child blocks
                text, _ = self.extractPageBlocks(block_id)
                content += text
                metadata[block_id]["text"] = text

            elif block["type"] == "table_row":
                text = self._extractTableRow(block["table_row"])
                content += text + "\n"
                metadata[block_id]["text"] = text

            else:
                print(f"Unsupported block type: {block['type']}, block: {block}")

        return content, metadata

    def queryDatabase_TwitterInbox(self, database_id, created_time=None):
        query_data = {
            "database_id": database_id,
        }

        # filter by created_time
        if created_time:
            query_data["filter"]["and"] = []
            query_data["filter"]["and"].append({
                "property": "Created time",
                "date": {
                    "on_or_after": created_time,
                }
            })

        pages = self.api.databases.query(**query_data).get("results")

        extracted_pages = {}
        for page in pages:
            print(f"result: page id: {page['id']}")

            page_id = page["id"]
            page_content, _ = self.extractPageBlocks(page_id)

            extracted_pages[page_id] = {
                "name": page["properties"]["Name"]["title"]["text"]["content"],
                "to": page["properties"]["To"]["rich_text"][0]["text"]["content"],
                # pdt timezone
                "created_at": page["properties"]["Created at"]["date"]["start"],
                "created_time": page["created_time"],
                "preview": page["properties"]["Preview"]["rich_text"][0]["text"]["content"],
                "notion_url": page["url"],
                "source": "Twitter",

                "content": page_content,
            }

        return extracted_pages

    def _createDatabaseItem_TwitterBase(self, list_names, tweet):
        """
        Create page properties and blocks
        """

        # assemble list name(s), sub-category of source, e.g.
        # The content from twitter and AI list
        source_list_names = [{"name": ln} for ln in list_names]
        tweet_url = f"https://twitter.com/{tweet['screen_name']}/status/{tweet['tweet_id']}"

        preview_content = tweet['text']
        if tweet["retweeted"]:
            preview_content = f"Retweeted: {preview_content}"

        properties = {
            "Name": {
                "title": [
                    {
                        "text": {
                            "content": f"{tweet['name']}"
                        }
                    }
                ]
            },

            "To": {
                "rich_text": [
                    {
                        "text": {
                            "content": tweet['reply_to_name'] if tweet['reply_to_name'] else ""
                        }
                    }
                ]
            },

            "Created at": {
                "date": {
                    "start": tweet['created_at_pdt'],
                    # "time_zone": "America/Los_Angeles",
                }
            },

            "Preview": {
                "rich_text": [
                    {
                        "text": {
                            "content": preview_content,
                            "link": {
                                "url": tweet_url,
                            }
                        },
                        "href": tweet_url,
                    },
                ]
            },

            "List Name": {
                "multi_select": source_list_names,
            },
        }

        block_content = f"{tweet['name']}"
        if tweet["retweeted"]:
            block_content += " (Retweeted)"

        block_content += f": {tweet['text']}"

        blocks = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": block_content
                            }
                        }
                    ]
                }
            }
        ]

        # append embeded content (if have)
        if tweet['embed']:
            blocks.append({
                "type": "embed",
                "embed": {
                    "url": tweet['embed']
                }
            })

        if tweet['reply_text']:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": f"Reply-to: {tweet['reply_to_name']}: {tweet['reply_text']}"
                            }
                        }
                    ]
                }
            })

            # assemble embeding content if it's in the replied content
            if tweet['reply_embed']:
                blocks.append({
                    "type": "embed",
                    "embed": {
                        "url": tweet['reply_embed']
                    }
                })

            # print(f"reply_tweet.url: {tweet['reply_embed']}")

        return properties, blocks

    def createDatabaseItem_TwitterInbox(self, database_id, list_names, tweet):
        """
        Create a page under a database
        database_id: the target notion database id
        tweet: the extracted tweet from TwitterAgent
        """
        properties, blocks = self._createDatabaseItem_TwitterBase(list_names, tweet)
        print(f"notion twitter inbox: database_id: {database_id}, properties: {properties}, blocks: {blocks}")

        # Add the new page to the database
        new_page = self.api.pages.create(
                parent={"database_id": database_id},
                properties=properties,
                children=blocks)

        return new_page

    def createDatabaseItem_ToRead(self, database_id, list_names: list, tweet, topics: list, categories: list, rate_number):
        properties, blocks = self._createDatabaseItem_TwitterBase(list_names, tweet)

        # assemble topics
        topics_list = [{"name": t} for t in topics]

        # assemble category (multi-select)
        categories_list = [{"name": c} for c in categories]

        properties["Source"] = {
            "rich_text": [
                {
                    "text": {
                        "content": "Twitter"
                    }
                }
            ]
        }

        properties.update({"Topic": {
            "multi_select": topics_list,
        }})

        properties.update({"Category": {
            "multi_select": categories_list,
        }})

        properties.update({"Rating": {
            "number": rate_number
        }})

        print(f"notion ToRead: database_id: {database_id}, properties: {properties}, blocks: {blocks}")

        # Add the new page to the database
        new_page = self.api.pages.create(
                parent={"database_id": database_id},
                properties=properties,
                children=blocks)

        # Try to add comments for user and reply_user
        try:
            page_id = new_page["id"]
            _, block_metadata = self.extractPageBlocks(page_id)

            print(f"Add user description as comment: {tweet['name']}, desc: {tweet['user_desc']}")
            self.createComment(block_metadata, tweet["name"], tweet["user_desc"])

            if tweet["reply_to_name"]:
                self.createComment(block_metadata, tweet["reply_to_name"], tweet["reply_user_desc"])

        except Exception as e:
            print(f"[ERROR] Failed to add comment: {e}")
            traceback.print_exc()

        return new_page

    def createComment(self, block_metadata, pattern: str, comment_text: str):
        for block_id, metadata in block_metadata.items():
            text = metadata["text"]

            if text.find(pattern) == -1:
                continue

            start = text.find(pattern)
            comment_range = {
                "start": start,
                "end": start + len(pattern),
            }

            new_comment = self.api.comments.create(
                block_id=block_id,
                text=[{
                    "type": "text",
                    "text": {
                        "content": comment_text
                    }
                }],
                visible_to="default",
                comment=comment_range
            )

            print(f"Created a new comment, pattern: {pattern}, comment: {comment_text}, new_comment object: {new_comment}")
