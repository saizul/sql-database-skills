class PostsController < ApplicationController
  def index
    @posts = Post.where("title LIKE '%#{params[:q]}%'")
  end
end
