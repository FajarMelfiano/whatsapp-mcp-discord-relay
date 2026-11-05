package main

import (
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"

	"bytes"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages. WAL keeps the relay's read-only
	// polls from blocking history-sync writes; one connection serializes all
	// writes (single-writer queue) and busy_timeout absorbs short contention.
	dsn := "file:store/messages.db?_foreign_keys=on&_journal_mode=WAL&_busy_timeout=10000&_synchronous=NORMAL"
	db, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	if _, err := db.Exec("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000; PRAGMA synchronous=NORMAL;"); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to configure message database: %v", err)
	}

	// Create tables if they don't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);
		
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}

	// Schema migrations for quoted messages and reactions. ALTER TABLE fails
	// with "duplicate column name" once the column exists, which is expected.
	migrations := []string{
		`ALTER TABLE messages ADD COLUMN quoted_id TEXT`,
		`ALTER TABLE messages ADD COLUMN quoted_sender TEXT`,
		`ALTER TABLE messages ADD COLUMN quoted_content TEXT`,
		`ALTER TABLE messages ADD COLUMN push_name TEXT`,
		`CREATE TABLE IF NOT EXISTS reactions (
			chat_jid TEXT,
			message_id TEXT,
			sender TEXT,
			emoji TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			PRIMARY KEY (chat_jid, message_id, sender)
		)`,
		`CREATE TABLE IF NOT EXISTS reaction_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			chat_jid TEXT,
			message_id TEXT,
			sender TEXT,
			emoji TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN
		)`,
		`ALTER TABLE messages ADD COLUMN mentioned_jids TEXT`,
		`ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0`,
		`ALTER TABLE messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0`,
		`CREATE TABLE IF NOT EXISTS message_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			chat_jid TEXT,
			message_id TEXT,
			kind TEXT,
			content TEXT,
			sender TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN
		)`,
		`CREATE TABLE IF NOT EXISTS receipt_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			chat_jid TEXT,
			message_id TEXT,
			receipt_type TEXT,
			timestamp TIMESTAMP
		)`,
		`CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, timestamp)`,
		`CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender)`,
		`CREATE INDEX IF NOT EXISTS idx_message_events_chat ON message_events(chat_jid, id)`,
		`CREATE INDEX IF NOT EXISTS idx_reaction_events_chat ON reaction_events(chat_jid, id)`,
	}
	for _, stmt := range migrations {
		if _, mErr := db.Exec(stmt); mErr != nil && !strings.Contains(mErr.Error(), "duplicate column name") {
			db.Close()
			return nil, fmt.Errorf("failed to migrate schema: %v", mErr)
		}
	}

	return &MessageStore{db: db}, nil
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, pushName, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64,
	quotedID, quotedSender, quotedContent, mentionedJIDs string) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}

	// Upsert instead of INSERT OR REPLACE: REPLACE deletes the old row and
	// inserts a new one, which changes rowid (making the Discord poller repost
	// old messages) and wipes fields captured live (notably push_name).
	_, err := store.db.Exec(
		`INSERT INTO messages
		(id, chat_jid, sender, push_name, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length, quoted_id, quoted_sender, quoted_content, mentioned_jids)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id, chat_jid) DO UPDATE SET
			sender = CASE WHEN excluded.sender IS NULL OR excluded.sender = '' THEN messages.sender ELSE excluded.sender END,
			push_name = COALESCE(NULLIF(excluded.push_name, ''), messages.push_name),
			content = CASE WHEN excluded.content IS NULL OR excluded.content = '' THEN messages.content ELSE excluded.content END,
			timestamp = excluded.timestamp,
			is_from_me = excluded.is_from_me,
			media_type = COALESCE(NULLIF(excluded.media_type, ''), messages.media_type),
			filename = COALESCE(NULLIF(excluded.filename, ''), messages.filename),
			url = COALESCE(NULLIF(excluded.url, ''), messages.url),
			media_key = COALESCE(excluded.media_key, messages.media_key),
			file_sha256 = COALESCE(excluded.file_sha256, messages.file_sha256),
			file_enc_sha256 = COALESCE(excluded.file_enc_sha256, messages.file_enc_sha256),
			file_length = CASE WHEN excluded.file_length > 0 THEN excluded.file_length ELSE messages.file_length END,
			quoted_id = COALESCE(NULLIF(excluded.quoted_id, ''), messages.quoted_id),
			quoted_sender = COALESCE(NULLIF(excluded.quoted_sender, ''), messages.quoted_sender),
			quoted_content = COALESCE(NULLIF(excluded.quoted_content, ''), messages.quoted_content),
			mentioned_jids = COALESCE(NULLIF(excluded.mentioned_jids, ''), messages.mentioned_jids)`,
		id, chatJID, sender, pushName, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
		quotedID, quotedSender, quotedContent, mentionedJIDs,
	)
	return err
}

// StoreMessageEvent records an edit/delete/poll-style event for the relay.
func (store *MessageStore) StoreMessageEvent(chatJID, messageID, kind, content, sender string, timestamp time.Time, isFromMe bool) error {
	_, err := store.db.Exec(
		`INSERT INTO message_events (chat_jid, message_id, kind, content, sender, timestamp, is_from_me)
		 VALUES (?, ?, ?, ?, ?, ?, ?)`,
		chatJID, messageID, kind, content, sender, timestamp, isFromMe,
	)
	return err
}

// StoreReceipt records a delivery/read receipt so the relay can mirror it.
func (store *MessageStore) StoreReceipt(chatJID, messageID, receiptType string, timestamp time.Time) error {
	_, err := store.db.Exec(
		`INSERT INTO receipt_events (chat_jid, message_id, receipt_type, timestamp)
		 VALUES (?, ?, ?, ?)`,
		chatJID, messageID, receiptType, timestamp,
	)
	return err
}

// UpdateMessageContent applies an edit to the stored message.
func (store *MessageStore) UpdateMessageContent(chatJID, messageID, content string) error {
	_, err := store.db.Exec(
		`UPDATE messages SET content = ?, edited = 1 WHERE chat_jid = ? AND id = ?`,
		content, chatJID, messageID,
	)
	return err
}

// MarkMessageDeleted hides a revoked message from history rebuilds.
func (store *MessageStore) MarkMessageDeleted(chatJID, messageID string) error {
	_, err := store.db.Exec(
		`UPDATE messages SET deleted = 1 WHERE chat_jid = ? AND id = ?`,
		chatJID, messageID,
	)
	return err
}

// BackfillPushName retroactively names older rows for a sender we just saw
// live. Unsaved contacts often have no row in whatsmeow_contacts; the
// per-message push name is the only identity available.
func (store *MessageStore) BackfillPushName(sender, pushName string) error {
	if sender == "" || pushName == "" {
		return nil
	}
	_, err := store.db.Exec(
		`UPDATE messages SET push_name = ?
		 WHERE sender IN (?, ?, ?) AND (push_name IS NULL OR push_name = '')`,
		pushName, sender, sender+"@lid", sender+"@s.whatsapp.net",
	)
	return err
}

// StoreReaction inserts, updates or removes a reaction on a message.
func (store *MessageStore) StoreReaction(chatJID, messageID, sender, emoji string, timestamp time.Time, isFromMe bool) error {
	if _, err := store.db.Exec(
		`INSERT INTO reaction_events (chat_jid, message_id, sender, emoji, timestamp, is_from_me)
		VALUES (?, ?, ?, ?, ?, ?)`,
		chatJID, messageID, sender, emoji, timestamp, isFromMe,
	); err != nil {
		return err
	}
	if emoji == "" {
		_, err := store.db.Exec(
			"DELETE FROM reactions WHERE chat_jid = ? AND message_id = ? AND sender = ?",
			chatJID, messageID, sender,
		)
		return err
	}
	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO reactions (chat_jid, message_id, sender, emoji, timestamp, is_from_me)
		VALUES (?, ?, ?, ?, ?, ?)`,
		chatJID, messageID, sender, emoji, timestamp, isFromMe,
	)
	return err
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// Extract text content from a message
func richResponseText(rich *waE2E.AIRichResponseMessage) string {
	if rich == nil {
		return ""
	}
	var parts []string
	for _, sub := range rich.GetSubmessages() {
		if sub == nil {
			continue
		}
		if t := strings.TrimSpace(sub.GetMessageText()); t != "" {
			parts = append(parts, t)
		}
		if latex := sub.GetLatexMetadata(); latex != nil {
			if t := strings.TrimSpace(latex.GetText()); t != "" {
				parts = append(parts, t)
			}
		}
		if code := sub.GetCodeMetadata(); code != nil {
			for _, block := range code.GetCodeBlocks() {
				if t := strings.TrimSpace(block.GetCodeContent()); t != "" {
					parts = append(parts, "```\n"+t+"\n```")
				}
			}
		}
	}
	return strings.Join(parts, "\n")
}

func pollText(poll *waE2E.PollCreationMessage) string {
	if poll == nil {
		return ""
	}
	lines := []string{"[poll] " + strings.TrimSpace(poll.GetName())}
	number := 0
	for _, opt := range poll.GetOptions() {
		name := strings.TrimSpace(opt.GetOptionName())
		if name != "" {
			number++
			lines = append(lines, fmt.Sprintf("%d. %s", number, name))
		}
	}
	return strings.Join(lines, "\n")
}

func locationText(loc *waE2E.LocationMessage) string {
	if loc == nil {
		return ""
	}
	name := strings.TrimSpace(loc.GetName())
	address := strings.TrimSpace(loc.GetAddress())
	label := strings.TrimSpace(strings.Join(nonEmpty(name, address), " — "))
	lat, lng := loc.GetDegreesLatitude(), loc.GetDegreesLongitude()
	link := fmt.Sprintf("https://maps.google.com/?q=%.6f,%.6f", lat, lng)
	if label == "" {
		return "[lokasi] " + link
	}
	return "[lokasi] " + label + " " + link
}

func vcardPhones(vcard string) []string {
	var phones []string
	for _, line := range strings.Split(vcard, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(strings.ToUpper(line), "TEL") {
			continue
		}
		if idx := strings.Index(line, ":"); idx >= 0 {
			phone := strings.TrimSpace(strings.ReplaceAll(line[idx+1:], " ", ""))
			if phone != "" {
				phones = append(phones, phone)
			}
		}
	}
	return phones
}

func contactText(contact *waE2E.ContactMessage) string {
	if contact == nil {
		return ""
	}
	name := strings.TrimSpace(contact.GetDisplayName())
	phones := vcardPhones(contact.GetVcard())
	label := strings.TrimSpace(strings.Join(nonEmpty(name, strings.Join(phones, ", ")), " — "))
	if label == "" {
		return "[kontak]"
	}
	return "[kontak] " + label
}

func nonEmpty(values ...string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			out = append(out, strings.TrimSpace(value))
		}
	}
	return out
}

func extractMentions(msg *waProto.Message) string {
	contextInfo := extractContextInfo(msg)
	if contextInfo == nil || len(contextInfo.GetMentionedJID()) == 0 {
		return ""
	}
	raw, err := json.Marshal(contextInfo.GetMentionedJID())
	if err != nil {
		return ""
	}
	return string(raw)
}

func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Unwrap containers used by multi-device, ephemeral and bot messages.
	if ds := msg.GetDeviceSentMessage(); ds != nil {
		if inner := ds.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}
	if inv := msg.GetBotInvokeMessage(); inv != nil {
		if inner := inv.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}
	if eph := msg.GetEphemeralMessage(); eph != nil {
		if inner := eph.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}
	if vo := msg.GetViewOnceMessage(); vo != nil {
		if inner := vo.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}
	if vo := msg.GetViewOnceMessageV2(); vo != nil {
		if inner := vo.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}
	if ed := msg.GetEditedMessage(); ed != nil {
		if inner := ed.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}
	if dc := msg.GetDocumentWithCaptionMessage(); dc != nil {
		if inner := dc.GetMessage(); inner != nil {
			return extractTextContent(inner)
		}
	}

	if text := msg.GetConversation(); text != "" {
		return text
	}
	if extended := msg.GetExtendedTextMessage(); extended != nil && extended.GetText() != "" {
		return extended.GetText()
	}
	if rich := msg.GetRichResponseMessage(); rich != nil {
		if text := richResponseText(rich); text != "" {
			return text
		}
	}
	if buttons := msg.GetButtonsMessage(); buttons != nil {
		if text := buttons.GetContentText(); text != "" {
			return text
		}
		if text := buttons.GetText(); text != "" {
			return text
		}
	}
	if interactive := msg.GetInteractiveMessage(); interactive != nil {
		if body := interactive.GetBody(); body != nil && body.GetText() != "" {
			return body.GetText()
		}
	}
	if response := msg.GetInteractiveResponseMessage(); response != nil {
		if body := response.GetBody(); body != nil && body.GetText() != "" {
			return body.GetText()
		}
	}
	if list := msg.GetListMessage(); list != nil {
		if text := list.GetTitle(); text != "" {
			return text
		}
		if text := list.GetDescription(); text != "" {
			return text
		}
	}
	if response := msg.GetListResponseMessage(); response != nil {
		if text := response.GetTitle(); text != "" {
			return text
		}
		if text := response.GetDescription(); text != "" {
			return text
		}
	}
	if response := msg.GetButtonsResponseMessage(); response != nil {
		if text := response.GetSelectedDisplayText(); text != "" {
			return text
		}
	}
	if response := msg.GetTemplateButtonReplyMessage(); response != nil {
		if text := response.GetSelectedDisplayText(); text != "" {
			return text
		}
	}
	if poll := msg.GetPollCreationMessage(); poll != nil {
		return pollText(poll)
	}
	if poll := msg.GetPollCreationMessageV2(); poll != nil {
		return pollText(poll)
	}
	if poll := msg.GetPollCreationMessageV3(); poll != nil {
		return pollText(poll)
	}
	if poll := msg.GetPollCreationMessageV5(); poll != nil {
		return pollText(poll)
	}
	if poll := msg.GetPollCreationMessageV6(); poll != nil {
		return pollText(poll)
	}
	if loc := msg.GetLocationMessage(); loc != nil {
		return locationText(loc)
	}
	if loc := msg.GetLiveLocationMessage(); loc != nil {
		return "[lokasi live]"
	}
	if contact := msg.GetContactMessage(); contact != nil {
		return contactText(contact)
	}
	if contacts := msg.GetContactsArrayMessage(); contacts != nil {
		var lines []string
		for _, contact := range contacts.GetContacts() {
			if text := contactText(contact); text != "" {
				lines = append(lines, text)
			}
		}
		if len(lines) > 0 {
			return strings.Join(lines, "\n")
		}
	}
	if img := msg.GetImageMessage(); img != nil && img.GetCaption() != "" {
		return img.GetCaption()
	}
	if vid := msg.GetVideoMessage(); vid != nil && vid.GetCaption() != "" {
		return vid.GetCaption()
	}
	if doc := msg.GetDocumentMessage(); doc != nil && doc.GetCaption() != "" {
		return doc.GetCaption()
	}

	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	MessageID string `json:"message_id,omitempty"`
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient    string `json:"recipient"`
	Message      string `json:"message"`
	MediaPath    string `json:"media_path,omitempty"`
	QuotedID     string `json:"quoted_message_id,omitempty"`
	QuotedSender string `json:"quoted_sender,omitempty"`
}

// ReactRequest represents the request body for the reaction API
type ReactRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	Emoji     string `json:"emoji"`
}

// ReactResponse represents the response for the reaction API
type ReactResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	MessageID string `json:"message_id,omitempty"`
}

type EditMessageRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	Message   string `json:"message"`
}

type DeleteMessageRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
}

type ReadMessagesRequest struct {
	ChatJID    string   `json:"chat_jid"`
	MessageIDs []string `json:"message_ids"`
	Sender     string   `json:"sender,omitempty"`
}

// attachQuote attaches quoted-message context to whichever message type was built.
func attachQuote(msg *waProto.Message, quotedID, quotedSender string) {
	contextInfo := &waProto.ContextInfo{StanzaID: proto.String(quotedID)}
	if quotedSender != "" {
		contextInfo.Participant = proto.String(quotedSender)
	}
	switch {
	case msg.Conversation != nil:
		text := msg.GetConversation()
		msg.Conversation = nil
		msg.ExtendedTextMessage = &waProto.ExtendedTextMessage{
			Text:        proto.String(text),
			ContextInfo: contextInfo,
		}
	case msg.ExtendedTextMessage != nil:
		msg.ExtendedTextMessage.ContextInfo = contextInfo
	case msg.ImageMessage != nil:
		msg.ImageMessage.ContextInfo = contextInfo
	case msg.VideoMessage != nil:
		msg.VideoMessage.ContextInfo = contextInfo
	case msg.AudioMessage != nil:
		msg.AudioMessage.ContextInfo = contextInfo
	case msg.DocumentMessage != nil:
		msg.DocumentMessage.ContextInfo = contextInfo
	}
}

// Function to send a WhatsApp message
func sendWhatsAppMessage(client *whatsmeow.Client, recipient string, message string, mediaPath string, quotedID string, quotedSender string) (bool, string, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp", ""
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err), ""
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err), ""
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.ToLower(mediaPath[strings.LastIndex(mediaPath, ".")+1:])
		var mediaType whatsmeow.MediaType
		var mimeType string

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type)
		default:
			mediaType = whatsmeow.MediaDocument
			mimeType = "application/octet-stream"
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err), ""
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err), ""
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		msg.Conversation = proto.String(message)
	}

	if quotedID != "" {
		attachQuote(msg, quotedID, quotedSender)
	}

	// Send message
	resp, err := client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err), ""
	}

	return true, fmt.Sprintf("Message sent to %s", recipient), string(resp.ID)
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}
	if ds := msg.GetDeviceSentMessage(); ds != nil {
		return extractMediaInfo(ds.GetMessage())
	}
	if eph := msg.GetEphemeralMessage(); eph != nil {
		return extractMediaInfo(eph.GetMessage())
	}
	if vo := msg.GetViewOnceMessage(); vo != nil {
		return extractMediaInfo(vo.GetMessage())
	}
	if vo := msg.GetViewOnceMessageV2(); vo != nil {
		return extractMediaInfo(vo.GetMessage())
	}
	if dc := msg.GetDocumentWithCaptionMessage(); dc != nil {
		return extractMediaInfo(dc.GetMessage())
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", "image_" + time.Now().Format("20060102_150405") + ".jpg",
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", "video_" + time.Now().Format("20060102_150405") + ".mp4",
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "audio_" + time.Now().Format("20060102_150405") + ".ogg",
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for sticker message (sent as a webp image)
	if sticker := msg.GetStickerMessage(); sticker != nil {
		return "sticker", "sticker_" + time.Now().Format("20060102_150405") + ".webp",
			sticker.GetURL(), sticker.GetMediaKey(), sticker.GetFileSHA256(), sticker.GetFileEncSHA256(), sticker.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := doc.GetFileName()
		if filename == "" {
			filename = "document_" + time.Now().Format("20060102_150405")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// extractContextInfo returns the ContextInfo carried by the message type, if any.
func extractContextInfo(msg *waProto.Message) *waProto.ContextInfo {
	if msg == nil {
		return nil
	}
	if inner := msg.GetDeviceSentMessage().GetMessage(); inner != nil {
		return extractContextInfo(inner)
	}
	if inner := msg.GetEphemeralMessage().GetMessage(); inner != nil {
		return extractContextInfo(inner)
	}
	if inner := msg.GetViewOnceMessage().GetMessage(); inner != nil {
		return extractContextInfo(inner)
	}
	if inner := msg.GetViewOnceMessageV2().GetMessage(); inner != nil {
		return extractContextInfo(inner)
	}
	if inner := msg.GetDocumentWithCaptionMessage().GetMessage(); inner != nil {
		return extractContextInfo(inner)
	}
	if inner := msg.GetEditedMessage().GetMessage(); inner != nil {
		return extractContextInfo(inner)
	}
	if m := msg.GetExtendedTextMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetImageMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetVideoMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetAudioMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetDocumentMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetStickerMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetContactMessage(); m != nil {
		return m.GetContextInfo()
	}
	if m := msg.GetLocationMessage(); m != nil {
		return m.GetContextInfo()
	}
	return nil
}

// extractQuotedInfo returns the quoted message ID, sender and a short preview.
func extractQuotedInfo(msg *waProto.Message) (quotedID, quotedSender, quotedContent string) {
	contextInfo := extractContextInfo(msg)
	if contextInfo == nil {
		return "", "", ""
	}
	quotedID = contextInfo.GetStanzaID()
	quotedSender = contextInfo.GetParticipant()
	if quotedMessage := contextInfo.GetQuotedMessage(); quotedMessage != nil {
		quotedContent = extractTextContent(quotedMessage)
		if quotedContent == "" {
			mediaType, filename, _, _, _, _, _ := extractMediaInfo(quotedMessage)
			if mediaType != "" {
				quotedContent = fmt.Sprintf("[%s] %s", mediaType, filename)
			}
		}
	}
	return quotedID, quotedSender, quotedContent
}

// messageTargetID extracts the original message ID from an edit/revoke
// protocol message, falling back to the event ID.
func messageTargetID(msg *events.Message) string {
	if protocol := msg.Message.GetProtocolMessage(); protocol != nil {
		if key := protocol.GetKey(); key != nil && key.GetID() != "" {
			return key.GetID()
		}
	}
	return string(msg.Info.ID)
}

// handleEdit applies a WhatsApp message edit to the store and emits an event.
func handleEdit(store *MessageStore, msg *events.Message, logger waLog.Logger) {
	target := messageTargetID(msg)
	content := ""
	if protocol := msg.Message.GetProtocolMessage(); protocol != nil {
		content = extractTextContent(protocol.GetEditedMessage())
	}
	if content == "" {
		content = extractTextContent(msg.Message)
	}
	if target == "" || content == "" {
		logger.Infof("Skipped empty edit %s in %s", msg.Info.ID, msg.Info.Chat.String())
		return
	}
	if err := store.UpdateMessageContent(msg.Info.Chat.String(), target, content); err != nil {
		logger.Warnf("Failed to update edited message: %v", err)
	}
	if err := store.StoreMessageEvent(msg.Info.Chat.String(), target, "edit", content,
		msg.Info.Sender.User, msg.Info.Timestamp, msg.Info.IsFromMe); err != nil {
		logger.Warnf("Failed to store edit event: %v", err)
	}
}

// handleRevoke marks a message deleted and emits a delete event.
func handleRevoke(store *MessageStore, msg *events.Message, logger waLog.Logger) {
	target := messageTargetID(msg)
	if target == "" {
		return
	}
	if err := store.MarkMessageDeleted(msg.Info.Chat.String(), target); err != nil {
		logger.Warnf("Failed to mark message deleted: %v", err)
	}
	if err := store.StoreMessageEvent(msg.Info.Chat.String(), target, "delete", "",
		msg.Info.Sender.User, msg.Info.Timestamp, msg.Info.IsFromMe); err != nil {
		logger.Warnf("Failed to store delete event: %v", err)
	}
}

// Handle regular incoming messages with media support
func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to database
	chatJID := msg.Info.Chat.String()
	sender := msg.Info.Sender.User
	pushName := msg.Info.PushName

	// Edits and revokes arrive as Message events with special attributes.
	if msg.Info.Edit == types.EditAttributeMessageEdit ||
		msg.Message.GetEditedMessage() != nil ||
		(msg.Message.GetProtocolMessage() != nil &&
			msg.Message.GetProtocolMessage().GetType() == waE2E.ProtocolMessage_MESSAGE_EDIT) {
		handleEdit(messageStore, msg, logger)
		return
	}
	if msg.Info.Edit == types.EditAttributeSenderRevoke || msg.Info.Edit == types.EditAttributeAdminRevoke {
		handleRevoke(messageStore, msg, logger)
		return
	}
	if protocol := msg.Message.GetProtocolMessage(); protocol != nil && protocol.GetType() == waE2E.ProtocolMessage_REVOKE {
		handleRevoke(messageStore, msg, logger)
		return
	}

	// Reactions are delivered as regular message events with a ReactionMessage.
	if reaction := msg.Message.GetReactionMessage(); reaction != nil {
		targetID := reaction.GetKey().GetID()
		if targetID == "" {
			return
		}
		reactionSender := sender
		if participant := reaction.GetKey().GetParticipant(); participant != "" {
			reactionSender = participant
		}
		if err := messageStore.StoreReaction(chatJID, targetID, reactionSender, reaction.GetText(), msg.Info.Timestamp, msg.Info.IsFromMe); err != nil {
			logger.Warnf("Failed to store reaction: %v", err)
		}
		return
	}

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Extract quoted message info (reply) and mentions
	quotedID, quotedSender, quotedContent := extractQuotedInfo(msg.Message)
	mentionedJIDs := extractMentions(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		logger.Infof("Skipped empty message %s in %s (rich=%v bot=%v)",
			msg.Info.ID, chatJID,
			msg.Message.GetRichResponseMessage() != nil,
			msg.Message.GetBotInvokeMessage() != nil)
		return
	}

	// Store message in database
	err = messageStore.StoreMessage(
		msg.Info.ID,
		chatJID,
		sender,
		pushName,
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
		quotedID,
		quotedSender,
		quotedContent,
		mentionedJIDs,
	)

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else {
		// A live message is the first time we can learn an unsaved member's
		// name; backfill older rows from the same sender.
		if pushName != "" {
			if err := messageStore.BackfillPushName(sender, pushName); err != nil {
				logger.Warnf("Failed to backfill push name: %v", err)
			}
		}
		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Log based on message type
		if mediaType != "" {
			fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
		} else if content != "" {
			fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
		}
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
	MaxBytes  int64  `json:"max_bytes,omitempty"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// safePathComponent keeps only a conservative character set so untrusted JIDs
// and filenames cannot escape the store directory.
func safePathComponent(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '-', r == '_', r == '.', r == '@':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	out := b.String()
	if out == "" || out == "." || out == ".." {
		return "_"
	}
	if len(out) > 200 {
		out = out[:200]
	}
	return out
}

// localMediaFilename builds the on-disk name from the message ID and a
// sanitized extension. The original filename is kept for display only.
func localMediaFilename(messageID, originalFilename string) string {
	base := safePathComponent(messageID)
	if base == "_" {
		base = "media"
	}

	var ext strings.Builder
	for _, r := range strings.ToLower(strings.TrimPrefix(filepath.Ext(originalFilename), ".")) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			ext.WriteRune(r)
			if ext.Len() >= 10 {
				break
			}
		}
	}
	if ext.Len() == 0 {
		return base + ".bin"
	}
	return base + "." + ext.String()
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string, maxBytes int64) (bool, string, string, string, error) {
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := filepath.Join("store", safePathComponent(chatJID))
	localPath := ""

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}
	if maxBytes <= 0 {
		maxBytes = 8 * 1024 * 1024
	}
	if fileLength > uint64(maxBytes) {
		return false, "", "", "", fmt.Errorf("media is %.1f MiB, limit is %.1f MiB",
			float64(fileLength)/1048576, float64(maxBytes)/1048576)
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Generate a local path for the file
	localPath = filepath.Join(chatDir, localMediaFilename(messageID, filename))

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Reuse an existing download only when it is non-empty and plausible.
	if info, statErr := os.Stat(localPath); statErr == nil {
		size := info.Size()
		if size > 0 && size <= maxBytes && (fileLength == 0 || uint64(size) == fileLength) {
			return true, mediaType, filename, absPath, nil
		}
		_ = os.Remove(localPath)
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image", "sticker":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	if int64(len(mediaData)) > maxBytes {
		return false, "", "", "", fmt.Errorf("media is %.1f MiB, limit is %.1f MiB",
			float64(len(mediaData))/1048576, float64(maxBytes)/1048576)
	}
	// Write to a temp file first so a failed download never leaves a partial
	// file at the final path.
	tmp, err := os.CreateTemp(chatDir, ".media-*")
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to create media temp file: %v", err)
	}
	tmpName := tmp.Name()
	keepTemp := true
	defer func() {
		if keepTemp {
			_ = os.Remove(tmpName)
		}
	}()
	if _, err := tmp.Write(mediaData); err != nil {
		_ = tmp.Close()
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}
	_ = tmp.Chmod(0644)
	if err := tmp.Close(); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}
	if err := os.Rename(tmpName, localPath); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}
	keepTemp = false

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Keep the query string: whatsmeow appends &hash=... to the direct path.
	return "/" + pathPart
}

// Start a REST API server to expose the WhatsApp client functionality
func loadBridgeToken() string {
	path := os.Getenv("BRIDGE_TOKEN_FILE")
	if path == "" {
		path = "../bridge.token"
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func requireAuth(token string, next http.Handler) http.Handler {
	if token == "" {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+token {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "Unauthorized",
			})
			return
		}
		next.ServeHTTP(w, r)
	})
}

func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, port int) {
	startedAt := time.Now()
	token := loadBridgeToken()
	protected := http.NewServeMux()

	// Handler for sending messages
	protected.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}

		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		fmt.Println("Received request to send message", req.Message, req.MediaPath)

		// Send the message
		success, message, messageID := sendWhatsAppMessage(client, req.Recipient, req.Message, req.MediaPath, req.QuotedID, req.QuotedSender)
		fmt.Println("Message sent", success, message)
		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Set appropriate status code
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}

		// Send response
		json.NewEncoder(w).Encode(SendMessageResponse{
			Success:   success,
			Message:   message,
			MessageID: messageID,
		})
	})

	// Handler for sending a reaction to a message
	protected.HandleFunc("/api/react", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req ReactRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "Chat JID and Message ID are required", http.StatusBadRequest)
			return
		}

		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("Invalid chat JID: %v", err), http.StatusBadRequest)
			return
		}
		if !client.IsConnected() || client.Store.ID == nil {
			http.Error(w, "Not connected to WhatsApp", http.StatusServiceUnavailable)
			return
		}

		reaction := client.BuildReaction(chatJID, *client.Store.ID, types.MessageID(req.MessageID), req.Emoji)
		resp, err := client.SendMessage(context.Background(), chatJID, reaction)

		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(ReactResponse{Success: false, Message: fmt.Sprintf("Error sending reaction: %v", err)})
			return
		}
		json.NewEncoder(w).Encode(ReactResponse{Success: true, Message: "Reaction sent", MessageID: string(resp.ID)})
	})

	// Handler for downloading media
	protected.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID, req.MaxBytes)

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	})

	// Request an on-demand history sync from the server. The response arrives
	// asynchronously as a HistorySync event and is stored by handleHistorySync.
	protected.HandleFunc("/api/history", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			ChatJID  string `json:"chat_jid"`
			BeforeID string `json:"before_id"`
			Count    int    `json:"count"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.ChatJID == "" {
			http.Error(w, "chat_jid is required", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if client == nil || !client.IsConnected() {
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "WhatsApp client is not connected",
			})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "Invalid chat_jid: " + err.Error(),
			})
			return
		}

		// The on-demand sync walks backwards from a known message. Use the
		// supplied before_id to page further back, otherwise start at the newest.
		var id string
		var isFromMe bool
		var unixTS int64
		if req.BeforeID != "" {
			err = messageStore.db.QueryRow(
				`SELECT id, is_from_me, CAST(strftime('%s', timestamp) AS INTEGER)
				 FROM messages WHERE chat_jid = ? AND id = ?`,
				req.ChatJID, req.BeforeID,
			).Scan(&id, &isFromMe, &unixTS)
		} else {
			err = messageStore.db.QueryRow(
				`SELECT id, is_from_me, CAST(strftime('%s', timestamp) AS INTEGER)
				 FROM messages WHERE chat_jid = ?
				 ORDER BY timestamp DESC, rowid DESC LIMIT 1`,
				req.ChatJID,
			).Scan(&id, &isFromMe, &unixTS)
		}
		if err != nil {
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "No stored message found for this chat",
			})
			return
		}

		if err := requestHistorySync(client, &types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:     chatJID,
				IsFromMe: isFromMe,
				IsGroup:  chatJID.Server == types.GroupServer,
			},
			ID:        id,
			Timestamp: time.Unix(unixTS, 0),
		}, req.Count); err != nil {
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": err.Error(),
			})
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":  true,
			"message":  "History sync requested",
			"chat_jid": req.ChatJID,
			"before":   id,
		})
	})

	// Read and write endpoints used by the relay for edits/deletes/receipts.
	protected.HandleFunc("/api/edit", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req EditMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "chat_jid, message_id and message are required", http.StatusBadRequest)
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil || !client.IsConnected() {
			http.Error(w, "Invalid chat or not connected", http.StatusServiceUnavailable)
			return
		}
		edit := client.BuildEdit(chatJID, types.MessageID(req.MessageID), &waProto.Message{
			Conversation: proto.String(req.Message),
		})
		if _, err := client.SendMessage(context.Background(), chatJID, edit); err != nil {
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
			return
		}
		_ = messageStore.UpdateMessageContent(req.ChatJID, req.MessageID, req.Message)
		_ = messageStore.StoreMessageEvent(req.ChatJID, req.MessageID, "edit", req.Message,
			client.Store.ID.User, time.Now(), true)
		json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "message": "Message edited"})
	})

	protected.HandleFunc("/api/delete", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req DeleteMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "chat_jid and message_id are required", http.StatusBadRequest)
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil || !client.IsConnected() {
			http.Error(w, "Invalid chat or not connected", http.StatusServiceUnavailable)
			return
		}
		if _, err := client.RevokeMessage(context.Background(), chatJID, types.MessageID(req.MessageID)); err != nil {
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
			return
		}
		_ = messageStore.MarkMessageDeleted(req.ChatJID, req.MessageID)
		_ = messageStore.StoreMessageEvent(req.ChatJID, req.MessageID, "delete", "",
			client.Store.ID.User, time.Now(), true)
		json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "message": "Message revoked"})
	})

	protected.HandleFunc("/api/read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req ReadMessagesRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" || len(req.MessageIDs) == 0 {
			http.Error(w, "chat_jid and message_ids are required", http.StatusBadRequest)
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil || !client.IsConnected() {
			http.Error(w, "Invalid chat or not connected", http.StatusServiceUnavailable)
			return
		}
		ids := make([]types.MessageID, 0, len(req.MessageIDs))
		for _, id := range req.MessageIDs {
			ids = append(ids, types.MessageID(id))
		}
		// Group receipts need the participant JID; direct chats keep EmptyJID.
		senderJID := types.EmptyJID
		if chatJID.Server == types.GroupServer && req.Sender != "" {
			senderJID, err = types.ParseJID(req.Sender)
			if err != nil {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{
					"success": false,
					"message": "Invalid sender JID: " + err.Error(),
				})
				return
			}
		}
		if err := client.MarkRead(context.Background(), ids, time.Now(), chatJID, senderJID); err != nil {
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "message": "Marked read"})
	})

	health := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		var dbBytes int64
		if info, err := os.Stat("store/messages.db"); err == nil {
			dbBytes = info.Size()
		}
		var messages, chats, maxRowID int64
		_ = messageStore.db.QueryRow("SELECT COUNT(*) FROM messages").Scan(&messages)
		_ = messageStore.db.QueryRow("SELECT COUNT(*) FROM chats").Scan(&chats)
		_ = messageStore.db.QueryRow("SELECT COALESCE(MAX(rowid),0) FROM messages").Scan(&maxRowID)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"ok":             client != nil && client.IsConnected(),
			"connected":      client != nil && client.IsConnected(),
			"uptime_seconds": int64(time.Since(startedAt).Seconds()),
			"db_bytes":       dbBytes,
			"messages":       messages,
			"chats":          chats,
			"max_rowid":      maxRowID,
		})
	})
	protected.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		var walBytes int64
		if info, err := os.Stat("store/messages.db-wal"); err == nil {
			walBytes = info.Size()
		}
		var lastMessage string
		_ = messageStore.db.QueryRow("SELECT COALESCE(MAX(timestamp), '') FROM messages").Scan(&lastMessage)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"ok":             client != nil && client.IsConnected(),
			"connected":      client != nil && client.IsConnected(),
			"uptime_seconds": int64(time.Since(startedAt).Seconds()),
			"wal_bytes":      walBytes,
			"last_message":   lastMessage,
			"goroutines":     runtime.NumGoroutine(),
		})
	})

	// Start the server: health is public, everything else requires the token.
	root := http.NewServeMux()
	root.HandleFunc("/api/health", health)
	root.Handle("/", requireAuth(token, protected))

	serverAddr := os.Getenv("BRIDGE_BIND")
	if serverAddr == "" {
		serverAddr = fmt.Sprintf("127.0.0.1:%d", port)
	}
	if token == "" {
		// Without a token every protected route is unauthenticated. Never
		// expose that state on a non-loopback interface.
		loopbackAddr := fmt.Sprintf("127.0.0.1:%d", port)
		if serverAddr != loopbackAddr {
			fmt.Printf("WARNING: no bridge token configured (BRIDGE_TOKEN_FILE missing or empty); ignoring BRIDGE_BIND=%q\n", serverAddr)
		}
		fmt.Printf("WARNING: REST API is unauthenticated; binding to loopback only on %s\n", loopbackAddr)
		serverAddr = loopbackAddr
	}
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	go func() {
		if err := http.ListenAndServe(serverAddr, root); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

func main() {
	// Set up logger
	logger := waLog.Stdout("Client", "WARN", true)
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", "INFO", true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on&_journal_mode=WAL&_busy_timeout=10000&_synchronous=NORMAL", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()

	// Setup event handling for messages and history sync
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)

		case *events.Receipt:
			for _, id := range v.MessageIDs {
				if err := messageStore.StoreReceipt(v.Chat.String(), string(id), string(v.Type), v.Timestamp); err != nil {
					logger.Warnf("Failed to store receipt: %v", err)
				}
			}

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please scan QR code to log in again")
		}
	})

	// Create channel to track connection success
	connected := make(chan bool, 1)

	// Connect to WhatsApp
	if client.Store.ID == nil {
		if phone := strings.TrimSpace(os.Getenv("WHATSAPP_PAIR_PHONE")); phone != "" {
			// Pair with phone number instead of scanning a QR code
			err = client.Connect()
			if err != nil {
				logger.Errorf("Failed to connect: %v", err)
				return
			}
			code, pairErr := client.PairPhone(context.Background(), phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
			if pairErr != nil {
				logger.Errorf("Failed to request pairing code: %v", pairErr)
				return
			}
			_ = os.WriteFile("/tmp/whatsapp_pair_code.txt", []byte(code), 0600)
			fmt.Printf("\nPAIRING CODE: %s\n", code)
			fmt.Println("Enter it in WhatsApp > Linked devices > Link with phone number.")
			deadline := time.Now().Add(3 * time.Minute)
			for !client.IsConnected() && time.Now().Before(deadline) {
				time.Sleep(time.Second)
			}
			if !client.IsConnected() {
				logger.Errorf("Timeout waiting for pairing code")
				return
			}
			fmt.Println("\nSuccessfully connected and authenticated!")
		} else {
			// No ID stored, this is a new client, need to pair with phone
			qrChan, _ := client.GetQRChannel(context.Background())
			err = client.Connect()
			if err != nil {
				logger.Errorf("Failed to connect: %v", err)
				return
			}

			// Print QR code for pairing with phone
			for evt := range qrChan {
				if evt.Event == "code" {
					_ = os.WriteFile("/tmp/whatsapp_qr_code.txt", []byte(evt.Code), 0600)
					fmt.Println("\nScan this QR code with your WhatsApp app:")
					qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
				} else if evt.Event == "success" {
					connected <- true
					break
				}
			}

			// Wait for connection
			select {
			case <-connected:
				fmt.Println("\nSuccessfully connected and authenticated!")
			case <-time.After(3 * time.Minute):
				logger.Errorf("Timeout waiting for QR code scan")
				return
			}
		}
	} else {
		// Already logged in, just connect
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
		connected <- true
	}

	// Wait a moment for connection to stabilize
	time.Sleep(2 * time.Second)

	if !client.IsConnected() {
		logger.Errorf("Failed to establish stable connection")
		return
	}

	fmt.Println("\n✓ Connected to WhatsApp! Type 'help' for commands.")

	// Start REST API server
	startRESTServer(client, messageStore, 8080)

	// Create a channel to keep the main goroutine alive
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	// Disconnect client
	client.Disconnect()
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil && existingName != "" {
		// Chat exists with a name, use that
		logger.Infof("Using existing chat name for %s: %s", chatJID, existingName)
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Using group name: %s", name)
	} else {
		// This is an individual contact
		logger.Infof("Getting name for contact: %s", chatJID)

		// Just use contact info (full name)
		contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
		if err == nil && contact.FullName != "" {
			name = contact.FullName
		} else if sender != "" {
			// Fallback to sender
			name = sender
		} else {
			// Last fallback to JID
			name = jid.User
		}

		logger.Infof("Using contact name: %s", name)
	}

	return name
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		chatJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				// Keep a month of history: the relay backfills 30 days and old
				// on-demand rows are needed to repair sender names.
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					if time.Since(time.Unix(int64(ts), 0)) > 30*24*time.Hour {
						continue
					}
				}

				// Extract text content
				content := extractTextContent(msg.Message.Message)

				// Extract media info
				var mediaType, filename, url string
				var mediaKey, fileSHA256, fileEncSHA256 []byte
				var fileLength uint64

				var quotedID, quotedSender, quotedContent, mentionedJIDs string
				if msg.Message.Message != nil {
					mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
					quotedID, quotedSender, quotedContent = extractQuotedInfo(msg.Message.Message)
					mentionedJIDs = extractMentions(msg.Message.Message)
				}

				// Log the message content for debugging
				logger.Infof("Message content: %v, Media Type: %v", content, mediaType)

				// Skip messages with no content and no media
				if content == "" && mediaType == "" {
					continue
				}

				// Determine sender. Always prefer the participant carried by the
				// message itself; falling back to the group JID turns every group
				// member into "120363...@g.us" and loses their name.
				isGroup := jid.Server == types.GroupServer
				pushName := msg.Message.GetPushName()
				var sender string
				isFromMe := false
				if msg.Message.Key != nil && msg.Message.Key.FromMe != nil {
					isFromMe = *msg.Message.Key.FromMe
				}
				if isFromMe {
					if client.Store.ID != nil {
						sender = client.Store.ID.User
					}
				} else if participant := msg.Message.GetParticipant(); participant != "" {
					sender = participant
				} else if msg.Message.Key != nil && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
					sender = *msg.Message.Key.Participant
				} else if !isGroup {
					sender = jid.User
				}

				// Store message
				msgID := ""
				if msg.Message.Key != nil && msg.Message.Key.ID != nil {
					msgID = *msg.Message.Key.ID
				}

				// Get message timestamp
				timestamp := time.Time{}
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					timestamp = time.Unix(int64(ts), 0)
				} else {
					continue
				}

				err = messageStore.StoreMessage(
					msgID,
					chatJID,
					sender,
					pushName,
					content,
					timestamp,
					isFromMe,
					mediaType,
					filename,
					url,
					mediaKey,
					fileSHA256,
					fileEncSHA256,
					fileLength,
					quotedID,
					quotedSender,
					quotedContent,
					mentionedJIDs,
				)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					// Log successful message storage
					if mediaType != "" {
						logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, mediaType, filename, content)
					} else {
						logger.Infof("Stored message: [%s] %s -> %s: %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, content)
					}
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
}

// Request history sync from the server.
//
// The server returns messages immediately BEFORE the supplied message, so the
// caller normally passes the newest stored message of the chat being repaired.
func requestHistorySync(client *whatsmeow.Client, lastKnown *types.MessageInfo, count int) error {
	if client == nil || lastKnown == nil {
		return fmt.Errorf("cannot request history sync without a last known message")
	}
	if !client.IsConnected() {
		return fmt.Errorf("client is not connected")
	}
	if client.Store.ID == nil {
		return fmt.Errorf("client is not logged in")
	}
	if count <= 0 {
		count = 100
	}
	if count > 500 {
		count = 500
	}

	historyMsg := client.BuildHistorySyncRequest(lastKnown, count)
	if historyMsg == nil {
		return fmt.Errorf("failed to build history sync request")
	}

	if _, err := client.SendPeerMessage(context.Background(), historyMsg); err != nil {
		fmt.Printf("Failed to request history sync: %v\n", err)
		return fmt.Errorf("failed to request history sync: %v", err)
	}
	fmt.Printf("History sync requested for %s before %s (%d messages).\n",
		lastKnown.Chat.String(), lastKnown.ID, count)
	return nil
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
