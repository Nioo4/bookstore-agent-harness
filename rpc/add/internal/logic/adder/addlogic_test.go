package adderlogic

import (
	"strings"
	"testing"

	"bookstore/rpc/add/adder"
)

func TestNormalizeAddRequestNewModel(t *testing.T) {
	book, err := normalizeAddRequest(&adder.AddReq{
		Isbn:       "9787111544937",
		Title:      "Redis实战",
		Author:     "Josiah Carlson",
		Price:      8800,
		StockCount: 25,
	})
	if err != nil {
		t.Fatal(err)
	}
	if book.ISBN != "9787111544937" || book.Title != "Redis实战" ||
		book.Author != "Josiah Carlson" || book.Price != 8800 || book.StockCount != 25 {
		t.Fatalf("unexpected normalized book: %+v", book)
	}
}

func TestNormalizeAddRequestLegacyCompatibility(t *testing.T) {
	book, err := normalizeAddRequest(&adder.AddReq{Book: "旧接口书名", Price: 1000})
	if err != nil {
		t.Fatal(err)
	}
	if book.Title != "旧接口书名" || book.Author != "Unknown" ||
		!strings.HasPrefix(book.ISBN, "legacy-") || book.Price != 1000 {
		t.Fatalf("unexpected legacy book: %+v", book)
	}
}

func TestNormalizeAddRequestRejectsInvalidInventory(t *testing.T) {
	_, err := normalizeAddRequest(&adder.AddReq{
		Isbn:       "9787111544937",
		Title:      "Redis实战",
		StockCount: -1,
	})
	if err == nil {
		t.Fatal("expected negative stock_count to be rejected")
	}
}
