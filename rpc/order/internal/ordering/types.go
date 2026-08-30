package ordering

import "time"

type ItemInput struct {
	BookID   int64 `json:"book_id"`
	Quantity int64 `json:"quantity"`
}

type Item struct {
	BookID    int64  `json:"book_id" db:"book_id"`
	ISBN      string `json:"isbn" db:"isbn_snapshot"`
	Title     string `json:"title" db:"title_snapshot"`
	UnitPrice int64  `json:"unit_price" db:"unit_price"`
	Quantity  int64  `json:"quantity" db:"quantity"`
	LineTotal int64  `json:"line_total" db:"line_total"`
}

type Preview struct {
	Items       []Item `db:"-"`
	TotalAmount int64
	Currency    string
	ExpiresAt   time.Time
}

type Order struct {
	OrderID     int64  `db:"order_id"`
	Status      string `db:"status"`
	Items       []Item
	TotalAmount int64     `db:"total_amount"`
	Currency    string    `db:"currency"`
	CreatedAt   time.Time `db:"created_at"`
	Replayed    bool      `db:"-"`
}

type OrderPage struct {
	Orders     []Order
	NextCursor string
}

type ConfirmationResult struct {
	ConfirmationID string
	Decision       string
	Status         string
	Order          *Order
	Replayed       bool
}
