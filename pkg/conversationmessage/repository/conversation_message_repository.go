package conversationmessage_repository

import (
	conversationmessage_model "github.com/EvolutionAPI/evolution-go/pkg/conversationmessage/model"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

type ConversationMessageRepository interface {
	// Upsert inserts a message, or silently does nothing if (chat_jid,
	// message_id) already exists -- the same row can safely be offered
	// twice (e.g. once live, once from a future history-sync backfill)
	// without ever producing a duplicate.
	Upsert(message conversationmessage_model.ConversationMessage) error
}

type conversationMessageRepository struct {
	db *gorm.DB
}

func (r *conversationMessageRepository) Upsert(message conversationmessage_model.ConversationMessage) error {
	return r.db.Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "chat_jid"}, {Name: "message_id"}},
		DoNothing: true,
	}).Create(&message).Error
}

func NewConversationMessageRepository(db *gorm.DB) ConversationMessageRepository {
	return &conversationMessageRepository{db: db}
}
