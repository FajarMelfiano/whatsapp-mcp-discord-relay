package main

import (
	"encoding/json"
	"strings"
	"testing"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestPollText(t *testing.T) {
	poll := &waE2E.PollCreationMessage{
		Name: proto.String("  Makan apa?  "),
		Options: []*waE2E.PollCreationMessage_Option{
			{OptionName: proto.String("Nasi goreng")},
			{OptionName: proto.String("  Mie ayam ")},
			{OptionName: proto.String("Sate")},
		},
	}

	got := pollText(poll)
	want := "[poll] Makan apa?\n1. Nasi goreng\n2. Mie ayam\n3. Sate"
	if got != want {
		t.Fatalf("pollText() = %q, want %q", got, want)
	}

	if got := pollText(nil); got != "" {
		t.Fatalf("pollText(nil) = %q, want empty", got)
	}
}

// Blank options are skipped and the visible options are numbered continuously.
func TestPollTextBlankOptionNumbering(t *testing.T) {
	poll := &waE2E.PollCreationMessage{
		Name: proto.String("Makan apa?"),
		Options: []*waE2E.PollCreationMessage_Option{
			{OptionName: proto.String("Nasi goreng")},
			{OptionName: proto.String("Mie ayam")},
			{OptionName: proto.String("   ")},
			{OptionName: proto.String("Sate")},
		},
	}

	got := pollText(poll)
	want := "[poll] Makan apa?\n1. Nasi goreng\n2. Mie ayam\n3. Sate"
	if got != want {
		t.Fatalf("pollText() = %q, want %q", got, want)
	}
}

func TestLocalMediaFilename(t *testing.T) {
	tests := []struct {
		messageID string
		filename  string
		want      string
	}{
		{"3EB0ABC123", "holiday.JPG", "3EB0ABC123.jpg"},
		{"../../etc/passwd", "../../evil.sh", ".._.._etc_passwd.sh"},
		{"msg/with:bad*chars", "report.tar.gz", "msg_with_bad_chars.gz"},
		{"no-extension", "README", "no-extension.bin"},
		{"weird.ext", "file.@#$%", "weird.ext.bin"},
		{"..", "x.png", "media.png"},
	}
	for _, tt := range tests {
		if got := localMediaFilename(tt.messageID, tt.filename); got != tt.want {
			t.Errorf("localMediaFilename(%q, %q) = %q, want %q", tt.messageID, tt.filename, got, tt.want)
		}
	}
}

func TestLocationText(t *testing.T) {
	loc := &waE2E.LocationMessage{
		DegreesLatitude:  proto.Float64(-6.175392),
		DegreesLongitude: proto.Float64(106.827153),
		Name:             proto.String(" Monas "),
		Address:          proto.String("Gambir, Jakarta Pusat"),
	}

	got := locationText(loc)
	for _, want := range []string{
		"[lokasi]",
		"Monas",
		"Gambir, Jakarta Pusat",
		"maps.google.com/?q=-6.175392,106.827153",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("locationText() = %q, missing %q", got, want)
		}
	}
	if !strings.Contains(got, "Monas — Gambir") {
		t.Fatalf("locationText() = %q, want name and address joined", got)
	}

	if got := locationText(nil); got != "" {
		t.Fatalf("locationText(nil) = %q, want empty", got)
	}
}

func TestVcardPhones(t *testing.T) {
	vcard := strings.Join([]string{
		"BEGIN:VCARD",
		"VERSION:3.0",
		"FN:Anna",
		"TEL;TYPE=CELL:+62 812-3456-7890",
		"TEL;TYPE=WORK:021 555 1234",
		"tel;type=home:+6281 1111 2222",
		"EMAIL:anna@example.com",
		"END:VCARD",
	}, "\r\n")

	got := vcardPhones(vcard)
	want := []string{"+62812-3456-7890", "0215551234", "+628111112222"}
	if len(got) != len(want) {
		t.Fatalf("vcardPhones() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("vcardPhones()[%d] = %q, want %q (all: %v)", i, got[i], want[i], got)
		}
	}

	if got := vcardPhones("FN:No Phone\nEND:VCARD"); len(got) != 0 {
		t.Fatalf("vcardPhones() = %v, want empty", got)
	}
}

func TestContactText(t *testing.T) {
	contact := &waE2E.ContactMessage{
		DisplayName: proto.String(" Anna "),
		Vcard: proto.String(strings.Join([]string{
			"BEGIN:VCARD",
			"VERSION:3.0",
			"FN:Anna",
			"TEL;TYPE=CELL:+62 812-3456-7890",
			"END:VCARD",
		}, "\n")),
	}

	got := contactText(contact)
	if !strings.Contains(got, "[kontak]") {
		t.Fatalf("contactText() = %q, want [kontak] label", got)
	}
	if !strings.Contains(got, "Anna") {
		t.Fatalf("contactText() = %q, want display name", got)
	}
	if !strings.Contains(got, "+62812-3456-7890") {
		t.Fatalf("contactText() = %q, want phone number", got)
	}

	if got := contactText(nil); got != "" {
		t.Fatalf("contactText(nil) = %q, want empty", got)
	}
}

func TestExtractMentions(t *testing.T) {
	msg := &waProto.Message{
		ExtendedTextMessage: &waProto.ExtendedTextMessage{
			Text: proto.String("hai @628111 @628222"),
			ContextInfo: &waProto.ContextInfo{
				MentionedJID: []string{"628111@s.whatsapp.net", "628222@s.whatsapp.net"},
			},
		},
	}

	got := extractMentions(msg)
	var mentions []string
	if err := json.Unmarshal([]byte(got), &mentions); err != nil {
		t.Fatalf("extractMentions() = %q, not valid JSON: %v", got, err)
	}
	want := []string{"628111@s.whatsapp.net", "628222@s.whatsapp.net"}
	if len(mentions) != len(want) {
		t.Fatalf("extractMentions() = %v, want %v", mentions, want)
	}
	for i := range want {
		if mentions[i] != want[i] {
			t.Fatalf("extractMentions()[%d] = %q, want %q", i, mentions[i], want[i])
		}
	}

	if got := extractMentions(nil); got != "" {
		t.Fatalf("extractMentions(nil) = %q, want empty", got)
	}
	if got := extractMentions(&waProto.Message{Conversation: proto.String("no mentions")}); got != "" {
		t.Fatalf("extractMentions(no context) = %q, want empty", got)
	}
}

func TestExtractTextContentPollAndContact(t *testing.T) {
	poll := &waProto.Message{
		PollCreationMessage: &waE2E.PollCreationMessage{
			Name: proto.String("Pilih minuman"),
			Options: []*waE2E.PollCreationMessage_Option{
				{OptionName: proto.String("Kopi")},
				{OptionName: proto.String("Teh")},
			},
		},
	}
	got := extractTextContent(poll)
	want := "[poll] Pilih minuman\n1. Kopi\n2. Teh"
	if got != want {
		t.Fatalf("extractTextContent(poll) = %q, want %q", got, want)
	}

	contact := &waProto.Message{
		ContactMessage: &waE2E.ContactMessage{
			DisplayName: proto.String("Budi"),
			Vcard:       proto.String("BEGIN:VCARD\nTEL;TYPE=CELL:+62 811-2222-3333\nEND:VCARD"),
		},
	}
	got = extractTextContent(contact)
	if !strings.Contains(got, "[kontak]") || !strings.Contains(got, "Budi") || !strings.Contains(got, "+62811-2222-3333") {
		t.Fatalf("extractTextContent(contact) = %q, want [kontak], name and phone", got)
	}

	if got := extractTextContent(nil); got != "" {
		t.Fatalf("extractTextContent(nil) = %q, want empty", got)
	}
}
