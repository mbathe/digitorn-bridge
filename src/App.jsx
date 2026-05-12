import React, { useState, useEffect } from 'react';
import Header from './components/Header';
import Hero from './components/Hero';
import ArticleGrid from './components/ArticleGrid';
import PhotoEssay from './components/PhotoEssay';
import Newsletter from './components/Newsletter';
import Footer from './components/Footer';
import SearchModal from './components/SearchModal';
import { articlesData } from './data/articles';
import './App.css';

function App() {
  const [articles, setArticles] = useState(articlesData);
  const [selectedCategory, setSelectedCategory] = useState('all');
  const [isSearchOpen, setIsSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [bookmarks, setBookmarks] = useState([]);

  // Charger les bookmarks depuis localStorage
  useEffect(() => {
    const savedBookmarks = localStorage.getItem('bookmarks');
    if (savedBookmarks) {
      setBookmarks(JSON.parse(savedBookmarks));
    }
  }, []);

  // Sauvegarder les bookmarks dans localStorage
  useEffect(() => {
    localStorage.setItem('bookmarks', JSON.stringify(bookmarks));
  }, [bookmarks]);

  // Filtrer les articles par catégorie
  const filteredArticles = selectedCategory === 'all' 
    ? articles 
    : articles.filter(article => article.category === selectedCategory);

  // Filtrer les articles par recherche
  const searchedArticles = searchQuery
    ? filteredArticles.filter(article =>
        article.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
        article.excerpt.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : filteredArticles;

  // Gérer les bookmarks
  const toggleBookmark = (articleId) => {
    setBookmarks(prev => 
      prev.includes(articleId)
        ? prev.filter(id => id !== articleId)
        : [...prev, articleId]
    );
  };

  // Ouvrir la recherche
  const handleSearchOpen = () => {
    setIsSearchOpen(true);
  };

  // Fermer la recherche
  const handleSearchClose = () => {
    setIsSearchOpen(false);
    setSearchQuery('');
  };

  return (
    <div className="app">
      <Header 
        onSearchClick={handleSearchOpen}
        bookmarkCount={bookmarks.length}
      />
      
      <main className="main-content">
        <Hero article={articles[0]} />
        
        <ArticleGrid 
          articles={searchedArticles}
          selectedCategory={selectedCategory}
          onCategoryChange={setSelectedCategory}
          bookmarks={bookmarks}
          onToggleBookmark={toggleBookmark}
        />
        
        <PhotoEssay />
        
        <Newsletter />
      </main>
      
      <Footer />
      
      <SearchModal
        isOpen={isSearchOpen}
        onClose={handleSearchClose}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        articles={searchedArticles}
        bookmarks={bookmarks}
        onToggleBookmark={toggleBookmark}
      />
    </div>
  );
}

export default App;
