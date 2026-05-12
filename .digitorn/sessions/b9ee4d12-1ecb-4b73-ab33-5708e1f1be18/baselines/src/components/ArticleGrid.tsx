import React, { useState } from 'react';
import ArticleCard from './ArticleCard';
import '../styles/ArticleGrid.css';

export interface Article {
  id: number;
  title: string;
  excerpt: string;
  category: string;
  categoryColor: string;
  image: string;
  author: string;
  authorAvatar: string;
  date: string;
  readTime: number;
  featured?: boolean;
}

const ArticleGrid: React.FC = () => {
  const [activeFilter, setActiveFilter] = useState('All');

  const articles: Article[] = [
    {
      id: 1,
      title: 'Crafting Traditions: The Revival of Artisan Pottery',
      excerpt: 'How modern ceramicists are breathing new life into ancient techniques, creating pieces that honor tradition while embracing contemporary design.',
      category: 'Culture',
      categoryColor: 'culture',
      image: 'https://images.unsplash.com/photo-1578749556568-bc2c40e68b61?w=800&h=600&fit=crop',
      author: 'Sofia Chen',
      authorAvatar: 'https://i.pravatar.cc/80?img=5',
      date: 'March 12, 2024',
      readTime: 6,
    },
    {
      id: 2,
      title: 'Farm to Table: A Journey Through Organic Agriculture',
      excerpt: 'Meet the farmers who are revolutionizing sustainable agriculture and bringing fresh, seasonal produce directly to our tables.',
      category: 'Food',
      categoryColor: 'food',
      image: 'https://images.unsplash.com/photo-1464226184884-fa280b87c399?w=800&h=600&fit=crop',
      author: 'Marcus Green',
      authorAvatar: 'https://i.pravatar.cc/80?img=12',
      date: 'March 10, 2024',
      readTime: 7,
    },
    {
      id: 3,
      title: 'Minimalist Spaces: The Beauty of Less',
      excerpt: 'Exploring how intentional design and thoughtful curation can transform living spaces into sanctuaries of calm and creativity.',
      category: 'Design',
      categoryColor: 'design',
      image: 'https://images.unsplash.com/photo-1618221195710-dd6b41faaea6?w=800&h=600&fit=crop',
      author: 'Aria Thompson',
      authorAvatar: 'https://i.pravatar.cc/80?img=9',
      date: 'March 8, 2024',
      readTime: 5,
    },
    {
      id: 4,
      title: 'Hidden Trails: Hiking Europe\'s Lesser-Known Paths',
      excerpt: 'Venture off the beaten track to discover serene mountain trails, ancient forests, and breathtaking vistas away from the crowds.',
      category: 'Travel',
      categoryColor: 'travel',
      image: 'https://images.unsplash.com/photo-1551632811-561732d1e306?w=800&h=600&fit=crop',
      author: 'Liam Foster',
      authorAvatar: 'https://i.pravatar.cc/80?img=13',
      date: 'March 5, 2024',
      readTime: 9,
    },
    {
      id: 5,
      title: 'The Forest Bathing Experience',
      excerpt: 'Understanding the Japanese practice of Shinrin-yoku and its profound effects on mental and physical well-being.',
      category: 'Nature',
      categoryColor: 'nature',
      image: 'https://images.unsplash.com/photo-1448375240586-882707db888b?w=800&h=600&fit=crop',
      author: 'Yuki Nakamura',
      authorAvatar: 'https://i.pravatar.cc/80?img=8',
      date: 'March 3, 2024',
      readTime: 6,
    },
    {
      id: 6,
      title: 'Sourdough Stories: The Ancient Art of Bread Making',
      excerpt: 'From starter to loaf, discover the meditative process of creating authentic sourdough bread in your own kitchen.',
      category: 'Food',
      categoryColor: 'food',
      image: 'https://images.unsplash.com/photo-1509440159596-0249088772ff?w=800&h=600&fit=crop',
      author: 'Pierre Dubois',
      authorAvatar: 'https://i.pravatar.cc/80?img=14',
      date: 'March 1, 2024',
      readTime: 8,
    },
  ];

  const categories = ['All', 'Travel', 'Culture', 'Food', 'Design', 'Nature'];

  const filteredArticles = activeFilter === 'All' 
    ? articles 
    : articles.filter(article => article.category === activeFilter);

  return (
    <section className="article-grid-section">
      <div className="container">
        <div className="section-header">
          <h2>Latest Stories</h2>
          <div className="category-filter">
            {categories.map(category => (
              <button
                key={category}
                className={`filter-btn ${activeFilter === category ? 'active' : ''}`}
                onClick={() => setActiveFilter(category)}
              >
                {category}
              </button>
            ))}
          </div>
        </div>

        <div className="article-grid">
          {filteredArticles.map(article => (
            <ArticleCard key={article.id} article={article} />
          ))}
        </div>

        <div className="load-more">
          <button className="btn-secondary">Load More Stories</button>
        </div>
      </div>
    </section>
  );
};

export default ArticleGrid;
