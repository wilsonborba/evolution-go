package conversationmessage_model

import (
	"time"
)

// ConversationMessage is the normalized, deduplicated record of an actual
// WhatsApp message (text, sender, chat), as opposed to evolution-go's own
// Message model (table "messages", GORM's default pluralization of that
// struct name) which only tracks delivery/read receipts. Explicit
// TableName below to avoid any ambiguity with that existing table.
type ConversationMessage struct {
	ID           uint64         `gorm:"primaryKey;autoIncrement"`
	MessageID    string         `gorm:"column:message_id;not null;uniqueIndex:idx_conversation_messages_chat_message"`
	ChatJID      string         `gorm:"column:chat_jid;not null;uniqueIndex:idx_conversation_messages_chat_message"`
	IsFromMe     *bool          `gorm:"column:is_from_me"`
	SenderJID    string         `gorm:"column:sender_jid"`
	PushName     string         `gorm:"column:push_name"`
	MessageType  string         `gorm:"column:message_type"`
	TextBody     string         `gorm:"column:text_body"`
	WaTimestamp  *time.Time     `gorm:"column:wa_timestamp"`
	CapturedVia  string         `gorm:"column:captured_via;not null"`
	Raw          string         `gorm:"column:raw;type:jsonb;not null"`
	InsertedAt   time.Time      `gorm:"column:inserted_at;not null;autoCreateTime"`
}

func (ConversationMessage) TableName() string {
	return "conversation_messages"
}
