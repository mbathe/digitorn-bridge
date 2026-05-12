import React, { useState, useEffect } from 'react';
import { X, Search as SearchIcon } from 'lucide-react';

const SearchModal = ({ isOpen, onClose, articles }) => {
  const [searchTerm, setSearchTerm] = useState('');
  const [results, setResults] = useState([]);

  useEffect(() => {
    if (!searchTerm.trim()) {
      setResults([]);
      return;
    }

    const filtered = articles.filter((article) =>
      article.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
      article.excerpt.toLowerCase().includes(searchTerm.toLowerCase()) ||
      article.category.toLowerCase().includes(searchTerm.toLowerCase())
    );

    setResults(filtered);
  }, [searchTerm, articles]);

  useEffect(() => {
    const handleEscape = (e) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };

    if (isOpen) {
      document.addEventListener('keydown', handleEscape);
      document.body.style.overflow = 'hidden';
    }

    return () => {
      document.removeEventListener('keydown', handleEscape);
      document.body.style.overflow = 'unset';
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div className="search-modal-overlay" onClick={onClose}>
      <div className="search-modal" onClick={(e) => e.stopPropagation()}>
        <div className="search-modal-header">
          <div className="search-input-wrapper">
            <SearchIcon size={20} />
            <input
              type="text"
              placeholder="Search articles..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              autoFocus
            />
          </div>
          <button className="close-button" onClick={onClose}>
            <X size={24} />
          </button>
        </div>

        <div className="search-results">
          {searchTerm && results.length === 0 && (
            <p className="no-results">No articles found for "{searchTerm}"</p>
          )}
          
          {results.map((article) => (
            <div key={article.id} className="search-result-item">
              <img src={article.image} alt={article.title} />
              <div className="search-result-content">
                <span className="search-result-category">{article.category}</span>
                <h3>{article.title}</h3>
                <p>{article.excerpt}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default SearchModal;
