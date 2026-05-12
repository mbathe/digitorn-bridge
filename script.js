// Products Database
const productsDatabase = [
    {
        id: 1,
        name: "Sculptural Armchair",
        category: "seating",
        price: 1200,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=500&h=600&fit=crop",
        badge: "New",
        colors: ["#D4B5A0", "#8B7355", "#2C2C2C"],
        description: "A bold statement piece combining comfort with artistic form. Hand-upholstered in premium fabric.",
        details: "Dimensions: 80cm W × 85cm D × 75cm H. Sustainably sourced oak frame with foam cushioning.",
        isFavorite: false,
        isNew: true
    },
    {
        id: 2,
        name: "Oak Dining Table",
        category: "tables",
        price: 2400,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1617806118233-18e1de247200?w=500&h=600&fit=crop",
        badge: null,
        colors: ["#8B7355", "#5C4033"],
        description: "Solid oak construction with natural oil finish. Seats 6-8 people comfortably.",
        details: "Dimensions: 200cm L × 95cm W × 75cm H. Handcrafted from sustainable European oak.",
        isFavorite: false,
        isNew: false
    },
    {
        id: 3,
        name: "Pendant Light Arc",
        category: "lighting",
        price: 380,
        originalPrice: 480,
        image: "https://images.unsplash.com/photo-1513506003901-1e6a229e2d15?w=500&h=600&fit=crop",
        badge: "-21%",
        colors: ["#D4AF37", "#2C2C2C", "#FFFFFF"],
        description: "Elegant brass pendant with adjustable height. Perfect for dining areas or entryways.",
        details: "Dimensions: 35cm diameter × 120cm max drop. E27 bulb (not included). Brass finish.",
        isFavorite: false,
        isNew: false
    },
    {
        id: 4,
        name: "Linen Throw Pillow",
        category: "textiles",
        price: 65,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1584100936595-c0654b55a2e2?w=500&h=600&fit=crop",
        badge: null,
        colors: ["#E8D5C4", "#C4A587", "#8B7355"],
        description: "Pure European linen with hidden zipper. Naturally textured and pre-washed.",
        details: "Dimensions: 50cm × 50cm. Machine washable. Feather insert included.",
        isFavorite: false,
        isNew: false
    },
    {
        id: 5,
        name: "Ceramic Vase Set",
        category: "objects",
        price: 145,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1578500494198-246f612d3b3d?w=500&h=600&fit=crop",
        badge: "Limited",
        colors: ["#E8D5C4", "#B8927D", "#FFFFFF"],
        description: "Hand-thrown ceramic vases with organic shapes. Set of three varying heights.",
        details: "Heights: 15cm, 20cm, 25cm. Glazed interior, matte exterior. Handmade variations.",
        isFavorite: false,
        isNew: false
    },
    {
        id: 6,
        name: "Lounge Chair",
        category: "seating",
        price: 890,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1567538096630-e0c55bd6374c?w=500&h=600&fit=crop",
        badge: null,
        colors: ["#8B7355", "#2C2C2C", "#D4B5A0"],
        description: "Mid-century inspired design with leather upholstery and walnut legs.",
        details: "Dimensions: 70cm W × 80cm D × 80cm H. Full-grain leather, oiled walnut frame.",
        isFavorite: false,
        isNew: false
    },
    {
        id: 7,
        name: "Console Table",
        category: "tables",
        price: 680,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1595428774223-ef52624120d2?w=500&h=600&fit=crop",
        badge: "New",
        colors: ["#8B7355", "#2C2C2C"],
        description: "Minimalist console with slim profile. Perfect for entryways or behind sofas.",
        details: "Dimensions: 120cm L × 35cm W × 75cm H. Solid ash with brass details.",
        isFavorite: false,
        isNew: true
    },
    {
        id: 8,
        name: "Table Lamp",
        category: "lighting",
        price: 220,
        originalPrice: null,
        image: "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?w=500&h=600&fit=crop",
        badge: null,
        colors: ["#D4AF37", "#FFFFFF", "#2C2C2C"],
        description: "Sculptural table lamp with fabric shade. Warm ambient lighting.",
        details: "Dimensions: 25cm diameter × 45cm H. E14 bulb (not included). Fabric shade.",
        isFavorite: false,
        isNew: false
    }
];

// State Management
let cart = JSON.parse(localStorage.getItem('cart')) || [];
let favorites = JSON.parse(localStorage.getItem('favorites')) || [];
let currentFilter = 'all';
let currentSort = 'featured';
let currentProduct = null;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

function initializeApp() {
    renderProducts();
    updateCartCount();
    updateFavoritesCount();
    attachEventListeners();
}

// Event Listeners
function attachEventListeners() {
    // Filter buttons
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', handleFilterClick);
    });

    // Sort select
    document.getElementById('sortSelect').addEventListener('change', handleSortChange);

    // Cart button
    document.getElementById('cartBtn').addEventListener('click', openCart);
    document.getElementById('closeCart').addEventListener('click', closeCart);
    document.getElementById('continueShoppingBtn').addEventListener('click', closeCart);

    // Favorites button
    document.getElementById('favoritesBtn').addEventListener('click', openFavorites);
    document.getElementById('closeFavorites').addEventListener('click', closeFavorites);

    // Modal
    document.getElementById('closeModal').addEventListener('click', closeModal);

    // Overlay
    document.getElementById('overlay').addEventListener('click', closeAll);

    // Newsletter form
    document.getElementById('newsletterForm').addEventListener('submit', handleNewsletterSubmit);

    // Search
    document.getElementById('searchBtn').addEventListener('click', handleSearch);
    document.getElementById('searchInput').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') handleSearch();
    });

    // Modal quantity controls
    document.getElementById('decreaseQty').addEventListener('click', () => changeQuantity(-1));
    document.getElementById('increaseQty').addEventListener('click', () => changeQuantity(1));

    // Modal actions
    document.getElementById('addToCartModal').addEventListener('click', addCurrentProductToCart);
    document.getElementById('addToFavModal').addEventListener('click', addCurrentProductToFavorites);
}

// Render Products
function renderProducts(products = productsDatabase) {
    const grid = document.getElementById('productsGrid');
    const filteredProducts = filterProducts(products);
    const sortedProducts = sortProducts(filteredProducts);
    
    grid.innerHTML = sortedProducts.map(product => createProductCard(product)).join('');
    
    // Update counts
    document.getElementById('showingCount').textContent = sortedProducts.length;
    document.getElementById('totalCount').textContent = productsDatabase.length;
    
    // Attach product card event listeners
    attachProductCardListeners();
}

function createProductCard(product) {
    const isFavorite = favorites.some(fav => fav.id === product.id);
    const discountPercent = product.originalPrice 
        ? Math.round(((product.originalPrice - product.price) / product.originalPrice) * 100)
        : null;

    return `
        <div class="product-card" data-id="${product.id}">
            ${product.badge ? `<span class="product-badge badge-${product.badge.toLowerCase().replace('%', '').replace('-', '')}">${product.badge}</span>` : ''}
            
            <div class="product-image" onclick="openProductModal(${product.id})">
                <img src="${product.image}" alt="${product.name}">
                <button class="quick-view-btn">Quick View</button>
            </div>
            
            <div class="product-info">
                <h3 class="product-name">${product.name}</h3>
                <div class="product-colors">
                    ${product.colors.map(color => `<span class="color-dot" style="background-color: ${color}"></span>`).join('')}
                </div>
                <div class="product-footer">
                    <div class="product-price">
                        ${product.originalPrice 
                            ? `<span class="price-original">€${product.originalPrice}</span>` 
                            : ''}
                        <span class="price-current">€${product.price}</span>
                    </div>
                    <button class="favorite-btn ${isFavorite ? 'active' : ''}" data-id="${product.id}" onclick="toggleFavorite(${product.id})">
                        ${isFavorite ? '♥' : '♡'}
                    </button>
                </div>
                <button class="btn btn-primary btn-full add-to-cart-btn" onclick="quickAddToCart(${product.id})">Add to Cart</button>
            </div>
        </div>
    `;
}

function attachProductCardListeners() {
    // Already using onclick in HTML for simplicity
}

// Filter Functions
function handleFilterClick(e) {
    document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
    e.target.classList.add('active');
    currentFilter = e.target.dataset.category;
    renderProducts();
}

function filterProducts(products) {
    if (currentFilter === 'all') return products;
    return products.filter(p => p.category === currentFilter);
}

// Sort Functions
function handleSortChange(e) {
    currentSort = e.target.value;
    renderProducts();
}

function sortProducts(products) {
    const sorted = [...products];
    
    switch(currentSort) {
        case 'price-asc':
            return sorted.sort((a, b) => a.price - b.price);
        case 'price-desc':
            return sorted.sort((a, b) => b.price - a.price);
        case 'name-asc':
            return sorted.sort((a, b) => a.name.localeCompare(b.name));
        case 'name-desc':
            return sorted.sort((a, b) => b.name.localeCompare(a.name));
        case 'newest':
            return sorted.sort((a, b) => b.isNew - a.isNew);
        default:
            return sorted;
    }
}

// Search Function
function handleSearch() {
    const query = document.getElementById('searchInput').value.toLowerCase().trim();
    
    if (!query) {
        renderProducts();
        return;
    }
    
    const results = productsDatabase.filter(p => 
        p.name.toLowerCase().includes(query) ||
        p.category.toLowerCase().includes(query) ||
        p.description.toLowerCase().includes(query)
    );
    
    renderProducts(results);
    
    if (results.length === 0) {
        document.getElementById('productsGrid').innerHTML = `
            <div class="no-results">
                <h3>No products found</h3>
                <p>Try searching with different keywords</p>
                <button class="btn btn-secondary" onclick="clearSearch()">Clear Search</button>
            </div>
        `;
    }
}

function clearSearch() {
    document.getElementById('searchInput').value = '';
    renderProducts();
}

// Favorites Functions
function toggleFavorite(id) {
    const product = productsDatabase.find(p => p.id === id);
    const index = favorites.findIndex(f => f.id === id);
    
    if (index > -1) {
        favorites.splice(index, 1);
        showNotification(`${product.name} removed from favorites`);
    } else {
        favorites.push(product);
        showNotification(`${product.name} added to favorites`);
    }
    
    saveFavorites();
    updateFavoritesCount();
    renderProducts();
}

function updateFavoritesCount() {
    document.getElementById('favCount').textContent = favorites.length;
}

function saveFavorites() {
    localStorage.setItem('favorites', JSON.stringify(favorites));
}

function openFavorites() {
    renderFavorites();
    document.getElementById('favoritesSidebar').classList.add('active');
    document.getElementById('overlay').classList.add('active');
}

function closeFavorites() {
    document.getElementById('favoritesSidebar').classList.remove('active');
    document.getElementById('overlay').classList.remove('active');
}

function renderFavorites() {
    const container = document.getElementById('favoritesItems');
    
    if (favorites.length === 0) {
        container.innerHTML = `
            <div class="empty-message">
                <p>No favorites yet</p>
                <button class="btn btn-secondary" onclick="closeFavorites()">Start Shopping</button>
            </div>
        `;
        return;
    }
    
    container.innerHTML = favorites.map(item => `
        <div class="cart-item">
            <img src="${item.image}" alt="${item.name}">
            <div class="cart-item-details">
                <h4>${item.name}</h4>
                <p class="cart-item-price">€${item.price}</p>
                <button class="btn btn-primary btn-small" onclick="quickAddToCart(${item.id}); closeFavorites();">Add to Cart</button>
            </div>
            <button class="remove-btn" onclick="toggleFavorite(${item.id})">×</button>
        </div>
    `).join('');
}

// Cart Functions
function quickAddToCart(id) {
    const product = productsDatabase.find(p => p.id === id);
    addToCart(product, 1);
}

function addToCart(product, quantity = 1) {
    const existingItem = cart.find(item => item.id === product.id);
    
    if (existingItem) {
        existingItem.quantity += quantity;
    } else {
        cart.push({ ...product, quantity });
    }
    
    saveCart();
    updateCartCount();
    showNotification(`${product.name} added to cart`);
}

function removeFromCart(id) {
    cart = cart.filter(item => item.id !== id);
    saveCart();
    updateCartCount();
    renderCart();
}

function updateCartItemQuantity(id, quantity) {
    const item = cart.find(item => item.id === id);
    if (item) {
        item.quantity = Math.max(1, quantity);
        saveCart();
        renderCart();
    }
}

function saveCart() {
    localStorage.setItem('cart', JSON.stringify(cart));
}

function updateCartCount() {
    const count = cart.reduce((sum, item) => sum + item.quantity, 0);
    document.getElementById('cartCount').textContent = count;
}

function openCart() {
    renderCart();
    document.getElementById('cartSidebar').classList.add('active');
    document.getElementById('overlay').classList.add('active');
}

function closeCart() {
    document.getElementById('cartSidebar').classList.remove('active');
    document.getElementById('overlay').classList.remove('active');
}

function renderCart() {
    const container = document.getElementById('cartItems');
    const totalEl = document.getElementById('cartTotal');
    
    if (cart.length === 0) {
        container.innerHTML = `
            <div class="empty-message">
                <p>Your cart is empty</p>
                <button class="btn btn-secondary" onclick="closeCart()">Continue Shopping</button>
            </div>
        `;
        totalEl.textContent = '€0.00';
        return;
    }
    
    const total = cart.reduce((sum, item) => sum + (item.price * item.quantity), 0);
    
    container.innerHTML = cart.map(item => `
        <div class="cart-item">
            <img src="${item.image}" alt="${item.name}">
            <div class="cart-item-details">
                <h4>${item.name}</h4>
                <p class="cart-item-price">€${item.price}</p>
                <div class="cart-quantity">
                    <button onclick="updateCartItemQuantity(${item.id}, ${item.quantity - 1})">−</button>
                    <span>${item.quantity}</span>
                    <button onclick="updateCartItemQuantity(${item.id}, ${item.quantity + 1})">+</button>
                </div>
            </div>
            <button class="remove-btn" onclick="removeFromCart(${item.id})">×</button>
        </div>
    `).join('');
    
    totalEl.textContent = `€${total.toFixed(2)}`;
}

function checkout() {
    if (cart.length === 0) {
        showNotification('Your cart is empty');
        return;
    }
    
    const total = cart.reduce((sum, item) => sum + (item.price * item.quantity), 0);
    alert(`Checkout functionality coming soon!\n\nYour order:\n${cart.map(item => `${item.name} x${item.quantity}`).join('\n')}\n\nTotal: €${total.toFixed(2)}`);
}

// Product Modal
function openProductModal(id) {
    currentProduct = productsDatabase.find(p => p.id === id);
    if (!currentProduct) return;
    
    document.getElementById('modalImage').src = currentProduct.image;
    document.getElementById('modalTitle').textContent = currentProduct.name;
    document.getElementById('modalPrice').textContent = currentProduct.originalPrice 
        ? `€${currentProduct.price} (was €${currentProduct.originalPrice})`
        : `€${currentProduct.price}`;
    document.getElementById('modalDescription').textContent = currentProduct.description;
    document.getElementById('modalDetails').textContent = currentProduct.details;
    
    // Render colors
    const colorsContainer = document.getElementById('modalColors');
    colorsContainer.innerHTML = currentProduct.colors.map(color => 
        `<span class="color-dot color-dot-large" style="background-color: ${color}"></span>`
    ).join('');
    
    // Reset quantity
    document.getElementById('quantityInput').value = 1;
    
    // Show modal
    document.getElementById('productModal').classList.add('active');
    document.getElementById('overlay').classList.add('active');
}

function closeModal() {
    document.getElementById('productModal').classList.remove('active');
    document.getElementById('overlay').classList.remove('active');
    currentProduct = null;
}

function changeQuantity(delta) {
    const input = document.getElementById('quantityInput');
    const newValue = Math.max(1, Math.min(10, parseInt(input.value) + delta));
    input.value = newValue;
}

function addCurrentProductToCart() {
    if (!currentProduct) return;
    const quantity = parseInt(document.getElementById('quantityInput').value);
    addToCart(currentProduct, quantity);
    closeModal();
}

function addCurrentProductToFavorites() {
    if (!currentProduct) return;
    toggleFavorite(currentProduct.id);
    closeModal();
}

// Newsletter
function handleNewsletterSubmit(e) {
    e.preventDefault();
    const email = document.getElementById('newsletterEmail').value;
    showNotification(`Thank you for subscribing with ${email}!`);
    document.getElementById('newsletterEmail').value = '';
}

// Utility Functions
function closeAll() {
    closeCart();
    closeFavorites();
    closeModal();
}

function scrollToProducts() {
    document.getElementById('productsSection').scrollIntoView({ behavior: 'smooth' });
}

function showNotification(message) {
    // Create notification element
    const notification = document.createElement('div');
    notification.className = 'notification';
    notification.textContent = message;
    document.body.appendChild(notification);
    
    // Trigger animation
    setTimeout(() => notification.classList.add('show'), 10);
    
    // Remove after 3 seconds
    setTimeout(() => {
        notification.classList.remove('show');
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}
